"""
Runs before test collection, so the required env vars exist before
signal_bot.py is imported anywhere (it reads DISCORD_WEBHOOK_URL at
module import time and will raise KeyError otherwise).
"""
import os

os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/test")
os.environ.setdefault("SIGNAL_TICKER", "TESTTICKER")
# Forced, not defaulted: the fixtures build 15-minute bars, so any other
# interval (the 1m default, or one sourced from .env) turns every bar into a
# session gap.
os.environ["SIGNAL_INTERVAL"] = "15m"
