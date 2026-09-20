"""
Resolution for the optional dependencies that live outside site-packages.

`requirements.txt` covers what `python -m winvoice` needs to run the assistant.
Two heavier, integration-only packages are deliberately *not* required to boot:
`mcp` (the bridge DeepSeek Harness calls tools through) and
`deepseek-harness-sdk` (the client that drives DSH). A user who never enables
DSH should not have to install a Node-runtime wrapper and an MCP SDK to say
「打开记事本」.

They are installed into `<repo>/.pylibs` (see `deployment.md`) and are put on
`sys.path` here, on demand, by the two modules that need them. The path is
**appended**, never prepended: `.pylibs` carries its own copies of pydantic and
anyio, and shadowing the versions the rest of the assistant was tested against
would be a far worse bug than the one this solves.
"""

from __future__ import annotations

import sys
from pathlib import Path

VENDOR_DIRNAME = ".pylibs"


def vendor_dir() -> Path:
    """`<repo>/.pylibs`, derived from this file rather than from the CWD."""
    return Path(__file__).resolve().parent.parent / VENDOR_DIRNAME


def ensure(module: str) -> None:
    """
    Make `module` importable, if it is not already.

    Never raises and never imports the module itself: the caller's own import
    statement stays the thing that reports a genuinely missing dependency, so
    the error message names the module the user actually needs.
    """
    try:
        __import__(module)
        return
    except ImportError:
        pass

    directory = vendor_dir()
    entry = str(directory)
    if directory.is_dir() and entry not in sys.path:
        sys.path.append(entry)


__all__ = ["VENDOR_DIRNAME", "ensure", "vendor_dir"]
