"""`python -m wizard` for development runs of the setup wizard."""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
