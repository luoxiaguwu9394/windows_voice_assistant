"""Entry point for ``python -m winvoice.enroll``."""

import asyncio

from .session import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
