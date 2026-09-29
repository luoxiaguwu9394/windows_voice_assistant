"""
One-click uninstall: remove the installation, its shortcuts and its processes.

What an install actually leaves behind (read off `flow.py`, `marker.py` and the
finish page, so this list is the same one the installer creates):

| Item | Where |
|---|---|
| runtime trees | `python/`, `winvoice/`, `scripts/`, `tools/`, `.pylibs/`, `config/config.template.yaml` |
| copied setup exe | `<install>/WinVoice-Setup-<version>.exe` |
| install marker | `<install>/.winvoice-install.json` |
| shortcuts | `WinVoice 语音助手.lnk` and `WinVoice 设置.lnk` on Desktop and in the Start Menu, plus a Startup `WinVoice 语音助手.lnk` when autostart was ticked |
| user data | `models/` (2–3 GB), `config/config.yaml`, `logs/`, `runtime/`, `snapshots/` |

The last row is the user's, so it is a **choice**: `keep_models` and
`keep_config` decide whether those survive. Environment variables the wizard
set with `setx` (`REMOTE_API_KEY`, `DEEPSEEK_API_KEY`) are deliberately left
alone — they are credentials the user may use elsewhere, and a silent
`setx` deletion is not something an uninstall should do on its own.

The install directory cannot be deleted while this wizard runs from inside it,
so the final removal is deferred to a detached PowerShell helper that retries:
the directory is gone a second or two after the window closes.
"""

from __future__ import annotations

import os
import base64
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from . import processes
from .marker import MARKER_NAME
from .shortcuts import (
    FOLDER_DESKTOP,
    FOLDER_STARTMENU,
    FOLDER_STARTUP,
    SETTINGS_SHORTCUT_NAME,
    SHORTCUT_NAME,
    special_folder,
)

# Never delete these: they are the user's, even when nothing is kept.
_USER_DATA = ("models", "config")

_DETACHED = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def removable_children(install_dir: Path, *, keep_models: bool, keep_config: bool) -> list[Path]:
    """
    The top-level entries an uninstall will delete, in a stable order.

    Kept trees are simply absent from the list, which is what makes the dry run
    in the wizard's confirmation text and the real deletion agree.
    """
    if not install_dir.is_dir():
        return []
    keep = set()
    if keep_models:
        keep.add("models")
    if keep_config:
        keep.add("config")
    return sorted(
        (entry for entry in install_dir.iterdir() if entry.name not in keep),
        key=lambda path: path.name.lower(),
    )


def _remove_shortcuts(log: Callable[[str], None]) -> list[str]:
    """Delete the shortcuts the installer created; report the ones that existed."""
    removed: list[str] = []
    names = (SHORTCUT_NAME, SETTINGS_SHORTCUT_NAME)
    for folder in (FOLDER_DESKTOP, FOLDER_STARTMENU, FOLDER_STARTUP):
        try:
            folder_path = special_folder(folder)
        except Exception:
            continue
        for name in names:
            path = folder_path / name
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
                    log(f"已删除快捷方式：{path}")
            except OSError as error:
                log(f"快捷方式删除失败（{path}）：{error}")
    return removed


def _remove_path(path: Path, log: Callable[[str], None]) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError as error:
        log(f"删除失败（{path}）：{error}")
        return False
    if path.exists():
        log(f"删除失败（仍在占用）：{path}")
        return False
    log(f"已删除：{path}")
    return True


def _deferred_removal(install_dir: Path) -> Path:
    """
    Write (and start) a detached PowerShell helper that deletes the install directory.

    A process cannot delete the directory its own executable lives in — and
    this wizard is `<install>/WinVoice-Setup-<version>.exe` — so the last step
    is handed to a detached shell that waits for the handles to go away. The
    loop retries for ~30 s: a virus scanner holding a freshly written .exe is
    exactly the case that needs the retry.
    """
    script = Path(tempfile.gettempdir()) / f"winvoice-uninstall-{os.getpid()}.ps1"
    target = str(install_dir.resolve()).replace("'", "''")
    script.write_text(
        f"$target = '{target}'\n"
        "for ($i = 0; $i -lt 30 -and (Test-Path -LiteralPath $target); $i++) {\n"
        "  try { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop } catch {}\n"
        "  if (Test-Path -LiteralPath $target) { Start-Sleep -Seconds 1 }\n"
        "}\n"
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue\n",
        encoding="utf-8",
    )
    encoded = base64.b64encode(
        f"& '{str(script).replace(chr(39), chr(39) * 2)}'".encode("utf-16le")
    ).decode("ascii")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        close_fds=True,
        creationflags=_DETACHED | _CREATE_NEW_PROCESS_GROUP,
    )
    return script


def uninstall(
    install_dir: Path,
    *,
    keep_models: bool = False,
    keep_config: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Remove the installation. Returns a summary dict; never raises.

    `keep_models` / `keep_config` keep the models and the generated
    `config/config.yaml` where they are — the directory then survives with just
    those, because deleting the user's 2–3 GB of models is not something an
    uninstall should decide on its own.
    """
    say = log or (lambda _message: None)
    summary: dict = {"stopped": [], "shortcuts": [], "removed": [], "failed": [], "deferred": False}

    say("停止正在运行的助手 / llama-server…")
    summary["stopped"] = processes.stop_occupants(install_dir, log=say)

    summary["shortcuts"] = _remove_shortcuts(say)

    for entry in removable_children(install_dir, keep_models=keep_models, keep_config=keep_config):
        if entry.name == MARKER_NAME:
            say("删除安装标记（卸载即不再被识别为已安装）")
        if _remove_path(entry, say):
            summary["removed"].append(str(entry))
        else:
            summary["failed"].append(str(entry))

    if summary["failed"]:
        say("有文件删不掉（多半仍被占用）——关闭相关程序后可手动删除该目录")
        return summary

    if keep_models or keep_config:
        kept = [
            name for name, keep in (("models", keep_models), ("config", keep_config))
            if keep and (install_dir / name).exists()
        ]
        # The marker is already gone — it is one of `removable_children` — and
        # leaving the directory without it is what stops a later wizard run from
        # offering "upgrade" for a husk that has no runtime left.
        say(f"保留：{'、'.join(kept) or '（无）'}（目录 {install_dir} 未删除）")
        return summary

    say("安排删除安装目录（向导自身就在其中，会在窗口关闭后完成）…")
    _deferred_removal(install_dir)
    summary["deferred"] = True
    return summary
