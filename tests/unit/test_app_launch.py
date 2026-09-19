"""
Regression: 「打开谷歌浏览器」 must actually open it, or say it could not.

Live report (2026-09-19):

    tool_result  success=True tool=open_app args={'app': '谷歌浏览器'}
    'chrome.exe' 不是内部或外部命令，也不是可运行的程序

The tool claimed success while cmd.exe was printing that it had no idea what
`chrome.exe` is. Two defects in one line of code:

1. `ALLOWED_APPS["chrome"] = "chrome.exe"` was launched as a *bare name* with
   `shell=True`. Chrome, Edge and VS Code are not on PATH — `shutil.which`
   returns None for all three on this machine, while Windows' own `App Paths`
   registry key knows exactly where they are.
2. `subprocess.Popen(..., shell=True)` cannot fail for a missing program: it
   starts cmd.exe, which prints the error and exits 1. The handler never looked,
   so 「已经打开谷歌浏览器了。」 was a lie.

These tests drive the real handlers with the launcher stubbed, so nothing is
actually opened and no registry-installed app is required.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from winvoice.tools import builtin
from winvoice.tools.builtin import (
    ALLOWED_APPS,
    close_app,
    open_app,
    resolve_app_command,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON = REPO_ROOT / "models" / "tts" / "vits-icefall-zh-aishell3" / "lexicon.txt"


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingSubprocess:
    """Records launch attempts; never starts anything."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises

    def _record(self, name: str, args: tuple, kwargs: dict):
        self.calls.append((name, args, kwargs))
        if self._raises is not None:
            raise self._raises
        return _FakeProc(self._returncode, self._stdout, self._stderr)

    def run(self, *args, **kwargs):
        return self._record("run", args, kwargs)

    def Popen(self, *args, **kwargs):
        return self._record("Popen", args, kwargs)

    @property
    def launched(self) -> bool:
        return bool(self.calls)


@pytest.fixture
def launcher(monkeypatch) -> _RecordingSubprocess:
    fake = _RecordingSubprocess()
    monkeypatch.setattr(builtin, "subprocess", fake)
    return fake


# ── locating the program ───────────────────────────────────────────────────

def test_a_command_on_path_is_located_by_the_filesystem():
    found = resolve_app_command("notepad.exe")

    assert found is not None and Path(found).exists(), found


def test_a_command_that_is_not_a_program_is_not_invented():
    assert resolve_app_command("definitely_not_installed_xyz.exe") is None
    assert resolve_app_command("") is None


@pytest.mark.parametrize("app", ["chrome", "edge", "vscode"])
def test_gui_apps_are_located_through_the_app_paths_registry(app):
    """
    None of these three is on PATH; `App Paths` is how Windows finds them.

    VS Code is the interesting one: it registers `Code.exe` while its command
    (and its PATH shim) is `code`, so the lookup needs the alias table.
    Skipped when the app is genuinely not installed — but then it must be
    *reported*, which `test_open_app_refuses_a_program_it_cannot_locate` pins.
    """
    resolved = resolve_app_command(ALLOWED_APPS[app])
    if resolved is None:
        pytest.skip(f"{ALLOWED_APPS[app]} is not installed on this machine")

    assert Path(resolved).exists(), resolved
    assert resolved.lower().endswith((".exe", ".cmd", ".bat")), resolved
    # The registry answer wins over the PATH shim, so a shim-less install works.
    registry_name = builtin._APP_PATHS_NAMES.get(ALLOWED_APPS[app], ALLOWED_APPS[app])
    if builtin._app_paths_entry(registry_name):
        assert Path(resolved).name.lower() == Path(builtin._app_paths_entry(registry_name)).name.lower()


def test_the_reported_breakage_is_a_resolution_problem_not_a_missing_app():
    """
    The user's log, reproduced: the bare name was unresolvable *while the
    program was installed*. Only true on a machine where that still holds, so
    it skips rather than failing when the environment has moved on.
    """
    if shutil.which(ALLOWED_APPS["chrome"]) is not None:
        pytest.skip("chrome.exe is on PATH here; the reported failure cannot be reproduced")
    if resolve_app_command(ALLOWED_APPS["chrome"]) is None:
        pytest.skip("Chrome is not installed here")

    assert Path(resolve_app_command(ALLOWED_APPS["chrome"])).exists()


# ── opening ────────────────────────────────────────────────────────────────

def test_open_app_launches_the_resolved_path_without_a_shell(monkeypatch, launcher):
    monkeypatch.setattr(builtin, "resolve_app_command", lambda command: r"C:\fake\chrome.exe")

    result = open_app({"app": "谷歌浏览器"})

    assert result["success"] is True, result
    assert result["message"] == "已经打开谷歌浏览器了。"
    assert launcher.launched, "nothing was launched"
    name, args, kwargs = launcher.calls[0]
    assert name == "Popen"
    assert args[0] == [r"C:\fake\chrome.exe"], args
    assert not kwargs.get("shell"), "a shell would swallow a missing-program error"


def test_open_app_refuses_a_program_it_cannot_locate(monkeypatch, launcher):
    """`chrome.exe` not on PATH means "I couldn't find it", never "opened"."""
    monkeypatch.setattr(builtin, "resolve_app_command", lambda command: None)

    result = open_app({"app": "谷歌浏览器"})

    assert result["success"] is False
    assert "谷歌浏览器" in result["message"], result
    assert not launcher.launched, "a program that was not found must not be launched"
    assert result["error"], "the machine-readable reason must survive for the log"


def test_open_app_reports_a_launch_that_the_os_rejected(monkeypatch):
    """A resolved-looking path that cannot be started is still a failure."""
    fake = _RecordingSubprocess(raises=OSError("not a valid Win32 application"))
    monkeypatch.setattr(builtin, "subprocess", fake)
    monkeypatch.setattr(builtin, "resolve_app_command", lambda command: r"C:\fake\chrome.exe")

    result = open_app({"app": "谷歌浏览器"})

    assert result["success"] is False
    assert "谷歌浏览器" in result["message"], result
    assert fake.launched


def test_open_app_still_uses_the_shell_only_for_uri_targets(monkeypatch, launcher):
    """`ms-settings:` is a URI, not a file: it needs `cmd /c start`."""
    result = open_app({"app": "系统设置"})

    assert result["success"] is True, result
    name, args, _ = launcher.calls[0]
    assert name == "run"
    assert args[0][:3] == ["cmd", "/c", "start"], args
    assert args[0][-1] == "ms-settings:"


# ── closing ────────────────────────────────────────────────────────────────

def test_close_app_reports_a_program_that_was_not_running(monkeypatch):
    """taskkill returns 128 when nothing matches: 「已经关闭」 would be a lie."""
    fake = _RecordingSubprocess(returncode=128, stderr="ERROR: The process not found.")
    monkeypatch.setattr(builtin, "subprocess", fake)

    result = close_app({"app": "记事本"})

    assert result["success"] is False, result
    assert "记事本" in result["message"] and "运行" in result["message"], result


def test_close_app_does_not_blame_a_permission_error_on_the_program(monkeypatch):
    """
    Only "no such process" means 「没有在运行」.

    Measured on this machine: killing the Store Notepad returns 1 with
    `ERROR: Access denied`, so reporting 「好像没有在运行」 would be wrong about
    the reason — the same class of false statement this whole file guards.
    """
    fake = _RecordingSubprocess(returncode=1, stderr="ERROR: Access denied")
    monkeypatch.setattr(builtin, "subprocess", fake)

    result = close_app({"app": "记事本"})

    assert result["success"] is False, result
    assert result["message"] == "我没能关掉记事本。", result
    assert "没有在运行" not in result["message"], result


def test_close_app_reports_success_when_the_process_was_killed(monkeypatch):
    fake = _RecordingSubprocess(returncode=0, stdout="SUCCESS: sent termination signal")
    monkeypatch.setattr(builtin, "subprocess", fake)

    result = close_app({"app": "记事本"})

    assert result["success"] is True, result
    assert result["message"] == "已经关闭记事本了。"


def test_close_app_admits_it_cannot_close_a_settings_uri(monkeypatch, launcher):
    result = close_app({"app": "系统设置"})

    assert result["success"] is False, result
    assert "系统设置" in result["message"], result
    assert not launcher.launched


# ── the spoken side ────────────────────────────────────────────────────────

@pytest.mark.skipif(not LEXICON.exists(), reason="TTS model not downloaded")
def test_every_new_app_message_the_handlers_produce_is_pronounceable(monkeypatch):
    """
    The messages are collected from the handlers, not copied: a string that
    drifts into English here would be dropped word by word by the TTS.
    """
    lexicon = {
        line.split()[0] for line in LEXICON.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    not_found = _RecordingSubprocess()
    not_running = _RecordingSubprocess(returncode=128, stderr="ERROR: The process not found.")
    refused = _RecordingSubprocess(returncode=1, stderr="ERROR: Access denied")

    monkeypatch.setattr(builtin, "resolve_app_command", lambda command: None)
    monkeypatch.setattr(builtin, "subprocess", not_found)
    spoken = [
        open_app({"app": "谷歌浏览器"})["message"],
        close_app({"app": "系统设置"})["message"],
    ]

    monkeypatch.setattr(builtin, "subprocess", not_running)
    spoken.append(close_app({"app": "记事本"})["message"])

    monkeypatch.setattr(builtin, "subprocess", refused)
    spoken.append(close_app({"app": "记事本"})["message"])

    for text in spoken:
        latin = "".join(c for c in text if c.isascii() and c.isalpha())
        assert not latin, f"{latin!r} would be dropped by the Chinese TTS: {text!r}"
        missing = sorted({c for c in text if c.isalpha() and not c.isascii() and c not in lexicon})
        assert not missing, f"{missing} are not in the lexicon: {text!r}"
        assert len(text) <= 80, f"too long to speak comfortably: {text!r}"
