"""
Unit tests for the setup wizard's core logic (`installer/wizard/core`).

The wizard is a separate artifact from the assistant, but its core is plain
Python under this repository so the same pytest run guards it. The most
valuable test here is the template one: if a token in
`config/config.template.yaml` stops matching what `state.py` provides (or the
template stops being valid YAML once rendered), the *installer* would ship
broken — these tests catch that drift without running the wizard.
"""

from __future__ import annotations

import shutil
import sys
import tarfile
import uuid
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALLER_DIR = PROJECT_ROOT / "installer"
if str(INSTALLER_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALLER_DIR))

from wizard.core import flow, marker, modelplan, payload, progress, systeminfo  # noqa: E402
from wizard.core import template as template_mod  # noqa: E402
from wizard.core import update, wakewords  # noqa: E402
from wizard.core.state import InstallState, default_config_values  # noqa: E402
from wizard.core.runner import OperationCancelled, run_streaming  # noqa: E402


@pytest.fixture
def work():
    """Scratch directory inside the repo (see test_dsh_router.py for why)."""
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ──────────────────────────────────────────────────────────────
# Template rendering
# ──────────────────────────────────────────────────────────────

class TestRenderTemplate:
    def test_inline_tokens_are_replaced_in_place(self):
        text = 'city: "@CITY@"  # comment\nrate: @RATE@\n'
        out = template_mod.render_template(text, {"CITY": "佛山", "RATE": "48000"})
        assert out == 'city: "佛山"  # comment\nrate: 48000\n'

    def test_block_token_replaces_the_whole_line(self):
        text = "keywords:\n@WORDS@\nthreshold: 0.25\n"
        out = template_mod.render_template(
            text, {"WORDS": '  - "你好助手"\n  - "assistant"'}
        )
        assert out == 'keywords:\n  - "你好助手"\n  - "assistant"\nthreshold: 0.25\n'

    def test_missing_value_raises_and_names_the_token(self):
        with pytest.raises(template_mod.TemplateError, match="CITY"):
            template_mod.render_template("x: @CITY@", {})

    def test_value_injected_token_is_rejected(self):
        # A value containing a token-looking string must never reach disk.
        with pytest.raises(template_mod.TemplateError, match="EVIL"):
            template_mod.render_template('x: "@CITY@"', {"CITY": "@EVIL@"})

    def test_trailing_newline_is_preserved(self):
        assert template_mod.render_template("a: @X@\n", {"X": "1"}).endswith("\n")
        assert not template_mod.render_template("a: @X@", {"X": "1"}).endswith("\n")


class TestKeywordBlock:
    def test_basic_block(self):
        block = template_mod.yaml_keyword_block(["你好助手", "assistant"])
        assert block == '  - "你好助手"\n  - "assistant"'

    def test_quotes_are_escaped(self):
        block = template_mod.yaml_keyword_block(['say "hi"'])
        assert block == '  - "say \\"hi\\""'

    def test_empty_list_is_rejected(self):
        with pytest.raises(template_mod.TemplateError):
            template_mod.yaml_keyword_block([])


class TestTemplateAgainstState:
    """The drift guard: the template and the state's values must agree."""

    def _template_text(self) -> str:
        return (PROJECT_ROOT / "config" / "config.template.yaml").read_text(encoding="utf-8")

    def test_defaults_render_cleanly_and_are_valid_yaml(self):
        rendered = template_mod.render_template(self._template_text(), default_config_values())
        data = yaml.safe_load(rendered)
        assert data["llm"]["local"]["model"] == "qwen2.5-3b-instruct"
        assert data["dsh"]["enabled"] is False  # off until the DSH step installs the bridge
        assert data["audio"]["input_device"] == "default"
        assert data["kws"]["keywords"] == ["小助手", "你好助手", "assistant"]

    def test_user_state_renders_cleanly_and_is_valid_yaml(self):
        state = InstallState(
            llm_tier="1.5b",
            audio_input="麦克风 (Realtek)",
            audio_output="扬声器 (Realtek)",
            output_rate=48000,
            output_host_api="wasapi",
            wake_words=["小助手", "hi there"],
            city="佛山",
            dsh_enabled=True,
            remote_key="sk-test",
        )
        rendered = template_mod.render_template(self._template_text(), state.config_values())
        data = yaml.safe_load(rendered)
        assert data["llm"]["local"]["model"] == "qwen2.5-1.5b-instruct"
        assert data["llm"]["local"]["model_file"].endswith("qwen2.5-1.5b-instruct-q4_k_m.gguf")
        assert data["dsh"]["enabled"] is True
        assert data["dsh"]["local"]["model"] == "qwen2.5-1.5b-instruct"
        assert data["llm"]["remote"]["enabled"] is True
        assert data["kws"]["keywords"] == ["小助手", "hi there"]
        assert data["weather"]["city"] == "佛山"
        assert data["audio"]["output_host_api"] == "wasapi"

    def test_every_template_token_is_known_to_state(self):
        from wizard.core.template import TOKEN_RE

        tokens = set(TOKEN_RE.findall(self._template_text()))
        assert tokens <= set(default_config_values())


# ──────────────────────────────────────────────────────────────
# Wake words
# ──────────────────────────────────────────────────────────────

class TestWakeWords:
    def test_trim_and_dedupe(self):
        accepted, warnings = wakewords.validate([" 小助手 ", "小助手", "", "assistant"])
        assert accepted == ["小助手", "assistant"]
        assert warnings == []

    def test_too_short_is_dropped_with_warning(self):
        accepted, warnings = wakewords.validate(["嗨", "小助手"])
        assert accepted == ["小助手"]
        assert len(warnings) == 1

    def test_too_long_is_dropped(self):
        accepted, warnings = wakewords.validate(["x" * 20])
        assert accepted == []
        assert warnings

    def test_punctuation_only_is_dropped(self):
        accepted, warnings = wakewords.validate(["!!!"])
        assert accepted == []
        assert warnings

    def test_cap_at_eight(self):
        words = [f"唤醒词{i:02d}" for i in range(10)]
        accepted, warnings = wakewords.validate(words)
        assert len(accepted) == 8
        assert warnings


# ──────────────────────────────────────────────────────────────
# Download progress protocol
# ──────────────────────────────────────────────────────────────

class Recorder(progress.DownloadEvents):
    def __init__(self):
        self.models = []
        self.progress = []
        self.results = []

    def on_model(self, key, note):
        self.models.append((key, note))

    def on_progress(self, key, done, total):
        self.progress.append((key, done, total))

    def on_result(self, key, ok, detail):
        self.results.append((key, ok, detail))


class TestFeedLine:
    def test_protocol_lines_dispatch(self):
        rec = Recorder()
        assert progress.feed_line("MODEL\tkws/x\t32.9 MB", rec) is True
        assert progress.feed_line("PROGRESS\tkws/x\t1024\t4096", rec) is True
        assert progress.feed_line("RESULT\tkws/x\tok\tdownloaded", rec) is True
        assert rec.models == [("kws/x", "32.9 MB")]
        assert rec.progress == [("kws/x", 1024, 4096)]
        assert rec.results == [("kws/x", True, "downloaded")]

    def test_human_lines_pass_through(self):
        rec = Recorder()
        assert progress.feed_line("  [OK] Saved: models/x", rec) is False
        assert progress.feed_line("", rec) is False
        assert not rec.models and not rec.results

    def test_malformed_progress_is_not_protocol(self):
        rec = Recorder()
        assert progress.feed_line("PROGRESS\tkws/x\tnot-a-number\t4096", rec) is False
        assert progress.feed_line("PROGRESS\tkws/x", rec) is False

    def test_failures_includes_missing_keys(self):
        failed = progress.failures({"a": (True, ""), "b": (False, "x")}, ["a", "b", "c"])
        assert failed == ["b", "c"]


class TestParseProbeResult:
    def test_parses_result_line(self):
        parsed = progress.parse_probe_result('RESULT {"ok": true, "sample_rate": 48000}')
        assert parsed == {"ok": True, "sample_rate": 48000}

    def test_garbage_is_none(self):
        assert progress.parse_probe_result("some log line") is None
        assert progress.parse_probe_result("RESULT not-json") is None
        assert progress.parse_probe_result('RESULT ["not","an","object"]') is None


# ──────────────────────────────────────────────────────────────
# Model plan
# ──────────────────────────────────────────────────────────────

class TestModelPlan:
    def test_core_keys_exclude_streaming_asr_by_default(self):
        keys = modelplan.model_keys("3b")
        assert "asr/zipformer" not in keys
        assert "llm/qwen2.5-3b-instruct" in keys

    def test_streaming_asr_can_be_added(self):
        assert "asr/zipformer" in modelplan.model_keys("3b", include_streaming_asr=True)

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError):
            modelplan.model_keys("7b")

    def test_download_argv_shape(self):
        argv = modelplan.download_argv("models", "0.5b")
        assert argv[0] == "scripts/download_models.py"
        assert "--progress-fmt" in argv and "machine" in argv
        only = argv[argv.index("--only") + 1]
        assert "llm/qwen2.5-0.5b-instruct" in only.split(",")


# ──────────────────────────────────────────────────────────────
# Update checks
# ──────────────────────────────────────────────────────────────

class TestUpdate:
    def test_version_tuple_ordering(self):
        assert update.version_tuple("0.2.1") > update.version_tuple("0.2.0")
        assert update.version_tuple("v1.0") > update.version_tuple("0.9.9")
        assert update.version_tuple("garbage") == (0, 0, 0)

    def test_is_newer(self):
        assert update.is_newer("0.2.0", "0.1.0")
        assert not update.is_newer("0.1.0", "0.1.0")
        assert not update.is_newer("0.0.9", "0.1.0")

    def test_parse_release_picks_the_setup_asset(self):
        info = update.parse_release(
            {
                "tag_name": "v0.2.0",
                "assets": [
                    {"name": "models.tar", "browser_download_url": "http://x/models.tar"},
                    {"name": "WinVoice-Setup-0.2.0.exe",
                     "browser_download_url": "http://x/WinVoice-Setup-0.2.0.exe"},
                ],
            },
            current_version="0.1.0",
        )
        assert info is not None
        assert info.version == "0.2.0"
        assert info.download_url.endswith(".exe")

    def test_parse_release_ignores_same_or_older(self):
        assert update.parse_release(
            {"assets": [{"name": "WinVoice-Setup-0.1.0.exe",
                         "browser_download_url": "http://x/f.exe"}]},
            current_version="0.1.0",
        ) is None

    def test_parse_release_without_asset_is_none(self):
        assert update.parse_release({"tag_name": "v9.9.9", "assets": []}, "0.1.0") is None


# ──────────────────────────────────────────────────────────────
# Install marker
# ──────────────────────────────────────────────────────────────

class TestMarker:
    def test_round_trip(self, work):
        directory = work / "install"
        assert marker.read_marker(directory) is None
        marker.write_marker(directory, version="0.1.0", check_updates=False)
        data = marker.read_marker(directory)
        assert data["version"] == "0.1.0"
        assert data["check_updates"] is False
        assert marker.marker_version(directory) == "0.1.0"

    def test_corrupt_marker_is_not_an_install(self, work):
        directory = work / "install"
        directory.mkdir(parents=True)
        (directory / marker.MARKER_NAME).write_text("not json", encoding="utf-8")
        assert marker.read_marker(directory) is None


# ──────────────────────────────────────────────────────────────
# System info
# ──────────────────────────────────────────────────────────────

class TestSystemInfo:
    def test_tier_recommendation(self):
        assert systeminfo.recommended_tier(16.0) == "3b"
        assert systeminfo.recommended_tier(14.0) == "3b"
        assert systeminfo.recommended_tier(8.0) == "1.5b"
        assert systeminfo.recommended_tier(4.0) == "0.5b"

    def test_ram_and_disk_report_something(self):
        assert systeminfo.total_ram_gb() > 0
        assert systeminfo.free_disk_gb(PROJECT_ROOT) > 0


# ──────────────────────────────────────────────────────────────
# Payload extraction
# ──────────────────────────────────────────────────────────────

class TestPayload:
    def _make_archive(self, work: Path) -> Path:
        staging = work / "staging"
        (staging / "winvoice").mkdir(parents=True)
        (staging / "winvoice" / "__init__.py").write_text("", encoding="utf-8")
        (staging / "config").mkdir(parents=True)
        (staging / "config" / "config.template.yaml").write_text("a: @X@\n", encoding="utf-8")
        archive = work / "payload.tar.xz"
        with tarfile.open(archive, "w:xz") as tar:
            tar.add(staging, arcname=".")
        return archive

    def test_extract_reports_progress_and_preserves_user_files(self, work):
        archive = self._make_archive(work)
        dest = work / "install"
        dest.mkdir()
        (dest / "config").mkdir()
        (dest / "config" / "config.yaml").write_text("user: data\n", encoding="utf-8")

        calls: list = []
        count = payload.extract_payload(
            archive, dest, on_progress=lambda done, total: calls.append((done, total))
        )
        assert count >= 3
        assert calls[-1][0] == calls[-1][1] > 0
        assert (dest / "winvoice" / "__init__.py").exists()
        assert (dest / "config" / "config.template.yaml").read_text(encoding="utf-8") == "a: @X@\n"
        # User data outside the payload is untouched.
        assert (dest / "config" / "config.yaml").read_text(encoding="utf-8") == "user: data\n"

    def test_missing_archive_raises(self, work):
        with pytest.raises(payload.PayloadError):
            payload.extract_payload(work / "nope.tar.xz", work / "install")


# ──────────────────────────────────────────────────────────────
# Subprocess runner
# ──────────────────────────────────────────────────────────────

class TestRunStreaming:
    def test_streams_lines_and_exit_code(self):
        lines: list = []
        code = run_streaming(
            [sys.executable, "-c", "print('alpha'); print('beta')"],
            cwd=PROJECT_ROOT, on_line=lines.append,
        )
        assert code == 0
        assert lines == ["alpha", "beta"]

    def test_stderr_is_merged(self):
        lines: list = []
        code = run_streaming(
            [sys.executable, "-c", "import sys; sys.stderr.write('oops\\n')"],
            cwd=PROJECT_ROOT, on_line=lines.append,
        )
        assert code == 0
        assert lines == ["oops"]

    def test_cancel_kills_the_child(self):
        with pytest.raises(OperationCancelled):
            run_streaming(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=PROJECT_ROOT, on_line=lambda line: None,
                cancel=lambda: True,
            )

    def test_nonzero_exit_code_is_returned(self):
        code = run_streaming(
            [sys.executable, "-c", "raise SystemExit(3)"],
            cwd=PROJECT_ROOT, on_line=lambda line: None,
        )
        assert code == 3


# ──────────────────────────────────────────────────────────────
# Config flow (write default + render user config)
# ──────────────────────────────────────────────────────────────

class TestConfigFlow:
    def _install_dir(self, work: Path) -> Path:
        install = work / "install"
        (install / "config").mkdir(parents=True)
        shutil.copy2(
            PROJECT_ROOT / "config" / "config.template.yaml",
            install / "config" / "config.template.yaml",
        )
        return install

    def test_write_default_config_creates_a_bootable_config(self, work):
        install = self._install_dir(work)
        target = flow.write_default_config(install)
        assert target is not None and target.is_file()
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert data["dsh"]["enabled"] is False
        assert data["llm"]["local"]["server_binary"] == (
            f"tools/llama-b7376-bin-win-cpu-x64/llama-server.exe"
        )
        # Idempotent: an existing config is never overwritten by defaults.
        target.write_text("keep: me\n", encoding="utf-8")
        assert flow.write_default_config(install) is None
        assert target.read_text(encoding="utf-8") == "keep: me\n"

    def test_render_user_config_backs_up_existing(self, work):
        install = self._install_dir(work)
        flow.write_default_config(install)
        state = InstallState(city="佛山", wake_words=["小助手"], llm_tier="3b")
        target = flow.render_user_config(install, state)
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert data["weather"]["city"] == "佛山"
        backups = list((install / "config").glob("config.yaml.bak-*"))
        assert len(backups) == 1


# ──────────────────────────────────────────────────────────────
# Env assembly
# ──────────────────────────────────────────────────────────────

class TestUpdateEnv:
    def test_proxy_is_set(self):
        state = InstallState(proxy_url="http://127.0.0.1:7890",
                             hf_endpoint="https://hf-mirror.com")
        env = flow.update_env(state, base={})
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
        assert env["NO_PROXY"] == "localhost,127.0.0.1"
        assert env["WINVOICE_HF_ENDPOINT"] == "https://hf-mirror.com"

    def test_direct_mode_drops_inherited_proxy(self):
        state = InstallState()
        env = flow.update_env(state, base={"HTTP_PROXY": "http://x:1", "HTTPS_PROXY": "http://x:1"})
        assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
