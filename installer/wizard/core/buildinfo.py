"""
Build-time metadata. `build_installer.py` rewrites VERSION (and may pin the
payload path) when freezing the wizard; the defaults below are what a
source-tree run sees.
"""

from __future__ import annotations

VERSION = "0.0.0+dev"

# Name of the payload archive bundled next to the wizard code (inside
# _MEIPASS for a frozen onefile build).
PAYLOAD_RELPATH = "payload/runtime.tar.xz"
