"""
Upgrade stalls, occupant processes, and uninstall.

Three things are pinned here, all from measured behaviour rather than taste:

* **the 92 % stall** — re-running the installer to upgrade froze the extraction
  where `tools/` begins (91.9 % of the payload's bytes are the trees before it).
  The blocker is `tools\\llama-b7376-bin-win-cpu-x64\\llama-server.exe`, held
  open by a llama-server the assistant *reuses but never owns*; `tarfile` raises
  `PermissionError`, which is not a `TarError`, so it escaped the extractor's
  error handling and surfaced as a modal dialog on the Tk thread — a frozen
  progress bar with no visible cause. It must come back as a `PayloadError`
  naming the file and the remedy;
* **who may be killed** — only processes whose executable lives inside the
  install directory, and never this process. `llama-server.exe` by image name is
  a fallback for when the question cannot be asked at all, because a user may be
  running their own server;
* **uninstall** — the runtime goes, the user's models and config are a choice,
  and the directory itself is deleted *after* this process exits (the wizard
  runs from inside it).

The wizard's core is plain Python in this repository, so the same pytest run
guards it. Scratch space lives in the repo (`runtime/_pytest_work`), never
`tmp_path` — see the note in `AGENTS.md`/`test_dsh_router.py`.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALLER_DIR = PROJECT_ROOT / "installer"
if str(INSTALLER_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALLER_DIR))

from wizard.core import marker, payload, processes, uninstall  # noqa: E402


@pytest.fixture
def work():
    """Scratch directory inside the repo (never the system temp dir)."""
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _archive_with(work: Path, name: str = "tools/llama-server.exe") -> Path:
    source = work / "src" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"X" * 512)
    archive = work / "p.tar.xz"
    with tarfile.open(archive, "w:xz") as tar:
        tar.add(source.parent, arcname=name.split("/")[0])
    return archive


# ──────────────────────────────────────────────────────────────
# Extraction: a locked file is named, and progress is visible
# ──────────────────────────────────────────────────────────────


class TestExtractLockedFile:
    def test_a_locked_file_is_named_in_the_error(self, work):
        archive = _archive_with(work)
        dest = work / "dest"
        original = tarfile.TarFile.extract

        def locked(self, member, *args, **kwargs):
            # A lock sits on a file, never on a directory member.
            if member.isfile():
                raise PermissionError(13, "Permission denied")
            return original(self, member, *args, **kwargs)

        tarfile.TarFile.extract = locked
        try:
            with pytest.raises(payload.PayloadError) as caught:
                payload.extract_payload(archive, dest)
        finally:
            tarfile.TarFile.extract = original

        message = str(caught.value)
        assert "tools/llama-server.exe" in message
        assert "关闭助手" in message
        assert "llama-server.exe" in message.split("该文件正被占用")[1]

    def test_progress_and_member_callbacks_fire(self, work):
        archive = _archive_with(work)
        dest = work / "dest"
        seen: list[str] = []
        fractions: list[tuple[int, int]] = []

        payload.extract_payload(archive, dest,
                                on_progress=lambda done, total: fractions.append((done, total)),
                                on_member=seen.append)

        assert seen and seen[0].endswith("llama-server.exe")
        assert fractions and fractions[-1][0] == fractions[-1][1] > 0

    def test_a_corrupt_archive_is_a_payload_error(self, work):
        archive = work / "broken.tar.xz"
        archive.write_bytes(b"not a tar at all")

        with pytest.raises(payload.PayloadError):
            payload.extract_payload(archive, work / "dest")

    def test_a_missing_archive_is_a_payload_error(self, work):
        with pytest.raises(payload.PayloadError):
            payload.extract_payload(work / "nope.tar.xz", work / "dest")


# ──────────────────────────────────────────────────────────────
# Occupants: never kill what cannot be proven ours
# ──────────────────────────────────────────────────────────────


class TestStopOccupants:
    def test_processes_in_the_install_dir_are_stopped(self, work, monkeypatch):
        killed: list[int] = []
        monkeypatch.setattr(processes, "processes_under",
                            lambda root: [(4321, "llama-server.exe", str(work))])
        monkeypatch.setattr(processes, "_kill_pid", lambda pid: killed.append(pid) or True)

        stopped = processes.stop_occupants(work, wait_s=0)

        assert killed == [4321]
        assert stopped and "llama-server.exe" in stopped[0]

    def test_this_process_is_never_killed(self, work, monkeypatch):
        killed: list[int] = []
        monkeypatch.setattr(processes, "processes_under",
                            lambda root: [(os.getpid(), "python.exe", str(work))])
        monkeypatch.setattr(processes, "_kill_pid", lambda pid: killed.append(pid) or True)

        assert processes.stop_occupants(work, wait_s=0) == []
        assert killed == []

    def test_the_image_name_fallback_runs_only_when_enumeration_failed(self, work, monkeypatch):
        images: list[str] = []
        monkeypatch.setattr(processes, "processes_under", lambda root: None)
        monkeypatch.setattr(processes, "_kill_image", lambda image: images.append(image) or False)

        processes.stop_occupants(work, wait_s=0)

        assert list(processes._OWNED_IMAGES) == images

    def test_a_visible_foreign_process_is_left_alone(self, work, monkeypatch):
        """Enumeration succeeded and found nothing of ours — so kill nothing."""
        images: list[str] = []
        monkeypatch.setattr(processes, "processes_under", lambda root: [])
        monkeypatch.setattr(processes, "_kill_image", lambda image: images.append(image) or True)

        assert processes.stop_occupants(work, wait_s=0) == []
        assert images == []

    def test_console_output_is_decoded_even_when_it_is_not_utf8(self):
        """`taskkill`/PowerShell answer in the ANSI codepage on a Chinese Windows."""
        gbk = "成功: 已终止 PID 为 4321 的进程".encode("gbk")

        assert "4321" in processes._decode(gbk)

    def test_undecodable_bytes_do_not_raise(self):
        assert processes._decode(b"\xff\xfe\x00\xb3") is not None

    def test_processes_under_returns_none_when_it_cannot_ask(self, monkeypatch):
        """None (cannot ask) and [] (nobody there) must stay distinguishable."""
        monkeypatch.setattr(processes, "_run", lambda command, timeout=20.0: (-1, ""))

        assert processes.processes_under(Path("C:/nope")) is None

    def test_process_path_sibling_is_not_inside_install_dir(self, work, monkeypatch):
        install = work / "WinVoice"
        sibling = work / "WinVoiceBackup" / "python.exe"
        owned = install / "python.exe"
        csv = (
            '"ProcessId","Name","ExecutablePath"\n'
            f'"10","python.exe","{sibling}"\n'
            f'"11","python.exe","{owned}"\n'
        )
        monkeypatch.setattr(processes, "_run", lambda *a, **k: (0, csv))

        assert processes.processes_under(install) == [(11, "python.exe", str(owned))]


# ──────────────────────────────────────────────────────────────
# Uninstall
# ──────────────────────────────────────────────────────────────


class TestUninstall:
    def _install(self, work: Path) -> Path:
        install = work / "WinVoice"
        for name in ("python", "tools", "winvoice", "scripts", "logs", "runtime", "snapshots"):
            (install / name).mkdir(parents=True, exist_ok=True)
            (install / name / "x.txt").write_text("x", encoding="utf-8")
        (install / "models").mkdir()
        (install / "models" / "big.onnx").write_bytes(b"M" * 256)
        (install / "config").mkdir()
        (install / "config" / "config.yaml").write_text("tts:\n  speed: 0.9\n", encoding="utf-8")
        (install / marker.MARKER_NAME).write_text("{}", encoding="utf-8")
        return install

    def test_kept_trees_are_not_in_the_removal_list(self, work):
        install = self._install(work)

        names = {p.name for p in uninstall.removable_children(
            install, keep_models=True, keep_config=True)}

        assert "models" not in names and "config" not in names
        assert "python" in names and "tools" in names

    def test_everything_goes_when_nothing_is_kept(self, work):
        install = self._install(work)

        names = {p.name for p in uninstall.removable_children(
            install, keep_models=False, keep_config=False)}

        assert {"models", "config", "python", "tools", marker.MARKER_NAME} <= names

    def test_keep_mode_removes_the_runtime_and_keeps_the_user_data(self, work, monkeypatch):
        install = self._install(work)
        monkeypatch.setattr(processes, "stop_occupants", lambda *a, **k: [])
        monkeypatch.setattr(uninstall, "_remove_shortcuts", lambda log: [])

        summary = uninstall.uninstall(install, keep_models=True, keep_config=True,
                                      log=lambda _m: None)

        assert summary["failed"] == []
        assert not summary["deferred"], "a kept directory must not be scheduled for removal"
        assert install.is_dir()
        assert (install / "models" / "big.onnx").is_file()
        assert (install / "config" / "config.yaml").is_file()
        assert not (install / "python").exists()
        assert not (install / marker.MARKER_NAME).exists(), (
            "the marker must go — it is what makes the next wizard run offer 'upgrade'"
        )

    def test_full_removal_is_deferred_to_a_helper(self, work, monkeypatch):
        """
        This wizard runs from inside the install directory, so it cannot delete
        that directory itself: the last step is handed to a detached shell.
        """
        install = self._install(work)
        monkeypatch.setattr(processes, "stop_occupants", lambda *a, **k: [])
        monkeypatch.setattr(uninstall, "_remove_shortcuts", lambda log: [])
        deferred: list[Path] = []
        monkeypatch.setattr(uninstall, "_deferred_removal", lambda path: deferred.append(path))

        summary = uninstall.uninstall(install, log=lambda _m: None)

        assert summary["deferred"] is True
        assert deferred == [install]
        assert install.is_dir(), "removal happens after this process exits"

    def test_a_stubborn_file_is_reported_rather_than_hidden(self, work, monkeypatch):
        install = self._install(work)
        monkeypatch.setattr(processes, "stop_occupants", lambda *a, **k: [])
        monkeypatch.setattr(uninstall, "_remove_shortcuts", lambda log: [])
        monkeypatch.setattr(uninstall, "_remove_path", lambda path, log: False)
        monkeypatch.setattr(uninstall, "_deferred_removal",
                            lambda path: pytest.fail("nothing may be deferred while files remain"))

        summary = uninstall.uninstall(install, log=lambda _m: None)

        assert summary["deferred"] is False
        assert summary["failed"], "an undeletable file has to be surfaced"

    def test_shortcuts_are_deleted_from_all_three_folders(self, work, monkeypatch):
        from wizard.core import shortcuts

        folders = {}
        for index, folder in enumerate(
                (shortcuts.FOLDER_DESKTOP, shortcuts.FOLDER_STARTMENU, shortcuts.FOLDER_STARTUP)):
            directory = work / f"folder{index}"
            directory.mkdir()
            (directory / shortcuts.SHORTCUT_NAME).write_text("", encoding="utf-8")
            folders[folder] = directory
        monkeypatch.setattr(uninstall, "special_folder", lambda folder: folders[folder])

        removed = uninstall._remove_shortcuts(lambda _m: None)

        assert len(removed) == 3
        assert all(not (directory / shortcuts.SHORTCUT_NAME).exists()
                   for directory in folders.values())

    def test_a_missing_shortcut_is_not_an_error(self, work, monkeypatch):
        monkeypatch.setattr(uninstall, "special_folder", lambda folder: work)

        assert uninstall._remove_shortcuts(lambda _m: None) == []

    def test_the_deferred_helper_targets_the_install_dir(self, work, monkeypatch):
        """The generated PowerShell helper uses a literal path and retries."""
        started: list[list[str]] = []
        monkeypatch.setattr(uninstall.subprocess, "Popen",
                            lambda command, **kwargs: started.append(command))

        script = uninstall._deferred_removal(work)

        text = script.read_text(encoding="utf-8")
        assert str(work) in text
        assert "Remove-Item -LiteralPath $target -Recurse -Force" in text
        assert "Start-Sleep" in text
        assert started and started[0][0] == "powershell"
        assert "-EncodedCommand" in started[0]
