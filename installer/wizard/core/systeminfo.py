"""System facts the wizard shows and reasons about — no UI, no side effects."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import platform
import shutil
import sys
from pathlib import Path

# RAM thresholds for picking the local LLM tier. Total RAM, not available:
# the wizard runs before anything big is resident. 16 GB machines report
# ~15.8 GB usable, hence 14 rather than 16.
TIER_THRESHOLDS = (("3b", 14.0), ("1.5b", 7.0))

# Space the wizard needs at the install dir, roughly: runtime payload
# (~850 MB extracted) + core models (~0.4 GB) + the largest LLM (~2.2 GB) +
# logs/runtime growth. Enforced as a warning below this.
MIN_DISK_GB = 6.0


def os_name() -> str:
    try:
        win = sys.getwindowsversion()
        return f"Windows {win.major}.{win.minor} build {win.build}"
    except Exception:  # pragma: no cover - non-Windows dev machines
        return platform.platform()


def total_ram_gb() -> float:
    """Physical memory via GlobalMemoryStatusEx (psutil is not bundled)."""

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.wintypes.DWORD),
            ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0.0
    return stat.ullTotalPhys / (1024**3)


def free_disk_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / (1024**3)
    except Exception:
        return 0.0


def recommended_tier(ram_gb: float) -> str:
    """'3b' at >=14 GB, '1.5b' at >=7 GB, else '0.5b'."""
    for tier, floor in TIER_THRESHOLDS:
        if ram_gb >= floor:
            return tier
    return "0.5b"
