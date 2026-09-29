"""Windows shortcut creation via WScript.Shell (pywin32, imported lazily)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# The two shortcut names the installer creates and the uninstaller deletes;
# kept here so both agree. The settings entry opens the web-based config UI.
SHORTCUT_NAME = "WinVoice 语音助手.lnk"
SETTINGS_SHORTCUT_NAME = "WinVoice 设置.lnk"

FOLDER_DESKTOP = "Desktop"
FOLDER_STARTMENU = "StartMenu"
FOLDER_STARTUP = "Startup"

# Fallbacks for the exotic case where WScript.Shell cannot be created at all.
_ENV_FALLBACKS = {
    FOLDER_DESKTOP: ("USERPROFILE", "Desktop"),
    FOLDER_STARTMENU: ("APPDATA", r"Microsoft\Windows\Start Menu"),
    FOLDER_STARTUP: ("APPDATA", r"Microsoft\Windows\Start Menu\Programs\Startup"),
}


def special_folder(name: str) -> Path:
    """The user's real Desktop / Start Menu / Startup folder."""
    try:
        from win32com.client import Dispatch  # noqa: PLC0415 - COM import is slow

        shell = Dispatch("WScript.Shell")
        value = shell.SpecialFolders(name)
        if value:
            return Path(value)
    except Exception:
        pass
    env_var, tail = _ENV_FALLBACKS[name]
    return Path(os.environ.get(env_var, "")) / tail


def create_shortcut(lnk_path: Path, target: str, arguments: str = "",
                    workdir: Optional[str] = None, icon_path: Optional[str] = None,
                    icon_index: int = 0, description: str = "") -> Path:
    """
    Write a .lnk file. The working directory matters as much as the target:
    the assistant resolves config/models relative to the CWD.
    """
    from win32com.client import Dispatch  # noqa: PLC0415

    lnk_path = Path(lnk_path)
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(lnk_path))
    shortcut.Targetpath = target
    if arguments:
        shortcut.Arguments = arguments
    if workdir:
        shortcut.WorkingDirectory = workdir
    if icon_path:
        shortcut.IconLocation = f"{icon_path},{icon_index}"
    if description:
        shortcut.Description = description
    shortcut.save()
    return lnk_path
