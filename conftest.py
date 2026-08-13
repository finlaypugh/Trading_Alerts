"""
Runs before test collection, so the required env vars exist before
signal_bot.py is imported anywhere (it reads DISCORD_WEBHOOK_URL at
module import time and will raise KeyError otherwise).
"""
import os

os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/test")
os.environ.setdefault("SIGNAL_TICKER", "TESTTICKER")
