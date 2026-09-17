"""
Speaker Enrollment Module Entry Point.

Usage: python -m winvoice.enroll --speaker me --samples 8
"""

from .enroll import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())