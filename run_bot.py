"""
Telegram bot entry point.

Usage:
    uv run run_bot.py
"""

import asyncio
from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())
