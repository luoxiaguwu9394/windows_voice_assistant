"""
Find the applications that actually exist on this machine.

The settings UI's 「扫描本机程序」 button runs this and offers the results as
additions to `tools.apps`. Two sources, both machine-adaptive by construction:

* **Start Menu shortcuts** — the user's and the common `Programs` trees. The
  .lnk target is resolved through the Windows shell (pywin32 COM), so a per-user
  install like WeChat lands here no matter which directory it chose at setup.
* **App Paths** — the registry keys installers write so `start chrome` works
  (HKCU then HKLM).

Uninstall/uninstaller stubs, non-existent targets and duplicates are filtered
out; what remains is worth offering a checkbox for.

The enumerators are injectable (`shortcuts` / `app_paths` parameters): tests
feed canned data, and only the thin default collectors touch the real machine.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

# Name fragments that mark an executable nobody wants to *open* by voice.
# "unins" covers Inno Setup's unins000.exe family, the most common stub.
_JUNK_RE = re.compile(r"unins|setup|install|eula|readme|help|crash|report", re.I)

ShortcutSource = Callable[[], list[tuple[str, str]]]  # (label, target path)
AppPathsSource = Callable[[], list[tuple[str, str]]]  # (registered name, path)


def scan_installed_apps(
    *,
    shortcuts: Optional[ShortcutSource] = None,
    app_paths: Optional[AppPathsSource] = None,
) -> list[dict]:
    """
    The deduplicated candidate list: `{"label", "path", "image"}` dicts.

    `label` is the human-visible name (shortcut stem / registered name), `path`
    the on-disk executable the settings UI stores as `command`, `image` the
    process basename. Sorted by label; safe to call from any thread.
    """
    resolve_shortcuts = shortcuts if shortcuts is not None else _from_start_menu
    read_app_paths = app_paths if app_paths is not None else _from_app_paths

    candidates: dict[str, dict] = {}

    def offer(label: str, path: str) -> None:
        path = path.strip().strip('"')
        if not path or not Path(path).is_absolute() or not Path(path).exists():
            return
        image = Path(path).name
        if not image.lower().endswith(".exe") or _JUNK_RE.search(image):
            return
        label = label.strip()
        if not label:
            return
        candidates.setdefault(path.lower(), {"label": label, "path": path, "image": image})

    try:
        for label, target in resolve_shortcuts():
            offer(label, target)
    except Exception:  # enumeration must never take the settings window down
        pass
    try:
        for _name, path in read_app_paths():
            offer(Path(path).stem, path)
    except Exception:
        pass

    return sorted(candidates.values(), key=lambda entry: entry["label"])


# ── default collectors (the only OS-touching code) ──────────────────────────


def _start_menu_dirs() -> list[Path]:
    dirs = []
    user = os.environ.get("APPDATA")
    common = os.environ.get("PROGRAMDATA")
    if user:
        dirs.append(Path(user) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if common:
        dirs.append(Path(common) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.is_dir()]


def _from_start_menu() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for directory in _start_menu_dirs():
        for lnk in directory.rglob("*.lnk"):
            target = _resolve_shortcut(lnk)
            if target:
                pairs.append((lnk.stem, target))
    return pairs


def _resolve_shortcut(lnk: Path) -> Optional[str]:
    """The .lnk's target path via the Windows shell, or None."""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()  # the caller may be a server worker thread
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            return str(shell.CreateShortCut(str(lnk)).Targetpath) or None
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        return None


def _from_app_paths() -> list[tuple[str, str]]:
    import winreg

    pairs: list[tuple[str, str]] = []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, subkey_name) as sub:
                            value = winreg.QueryValueEx(sub, "")[0]
                    except OSError:
                        continue
                    if value:
                        pairs.append((subkey_name, str(value).strip().strip('"')))
        except OSError:
            continue
    return pairs
