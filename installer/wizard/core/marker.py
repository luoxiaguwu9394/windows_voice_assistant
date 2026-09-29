"""
The install marker: what makes an install dir re-enterable and upgradable.

`<install>/.winvoice-install.json` is written after a successful install. Its
presence says "this directory is a WinVoice install of version X" — the wizard
uses it for the re-entry menu (重新配置 / 修复运行时 / 启动) and the update
check compares versions against it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

MARKER_NAME = ".winvoice-install.json"
LAYOUT_VERSION = 1


def marker_path(install_dir: Path) -> Path:
    return install_dir / MARKER_NAME


def discover_install_dir(default: Path, *, executable: Optional[Path] = None,
                         override: Optional[str] = None) -> Path:
    """Resolve the installation this setup executable should manage.

    A command-line override is used when an update is launched from Downloads.
    Otherwise, a copied setup executable next to a valid marker manages its
    own installation; a freshly downloaded installer falls back to the default.
    """
    if override:
        return Path(override).expanduser()
    if executable is not None:
        candidate = executable.resolve().parent
        if read_marker(candidate) is not None:
            return candidate
    return default


def read_marker(install_dir: Path) -> Optional[dict]:
    """The parsed marker, or None when `install_dir` is not an install."""
    path = marker_path(install_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(install_dir: Path, version: str, check_updates: bool = True) -> dict:
    install_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "version": version,
        "layout": LAYOUT_VERSION,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "check_updates": bool(check_updates),
    }
    marker_path(install_dir).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def marker_version(install_dir: Path) -> Optional[str]:
    data = read_marker(install_dir)
    return data.get("version") if data else None
