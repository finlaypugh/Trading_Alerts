#!/usr/bin/env python3
"""
Standalone trading signal bot.

Polls price data on a schedule, computes an EMA9/EMA21 crossover filtered
by RSI, and posts a Discord alert when a new signal appears. This script
never places trades — it only sends notifications for you to act on
manually.

Setup:
    pip install yfinance pandas requests
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python signal_bot.py

Config is via environment variables (see the block below) so you can run
multiple instances for different tickers without editing the file.
"""

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ---- Config (override via environment variables) ----
TICKER = os.environ.get("SIGNAL_TICKER", "AAPL")            # Yahoo Finance ticker symbol
INTERVAL = os.environ.get("SIGNAL_INTERVAL", "15m")          # 1m,5m,15m,1h,1d ...
LOOKBACK = os.environ.get("SIGNAL_LOOKBACK", "5d")           # history window to pull each poll
FAST_EMA = int(os.environ.get("SIGNAL_FAST_EMA", 9))
SLOW_EMA = int(os.environ.get("SIGNAL_SLOW_EMA", 21))
RSI_LEN = int(os.environ.get("SIGNAL_RSI_LEN", 14))
RSI_MIN = float(os.environ.get("SIGNAL_RSI_MIN", 40))
RSI_MAX = float(os.environ.get("SIGNAL_RSI_MAX", 70))
POLL_SECONDS = int(os.environ.get("SIGNAL_POLL_SECONDS", 900))  # 15 min default
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]         # required, no default

STATE_FILE = Path(__file__).parent / f".state_{TICKER}.json"


def load_last_signal():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("last_signal")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_last_signal(signal):
    STATE_FILE.write_text(json.dumps({"last_signal": signal}))


def compute_indicators(df):
    df = df.copy()
    df["ema_fast"] = df["Close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=SLOW_EMA, adjust=False).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


MIN_BARS = max(SLOW_EMA, RSI_LEN) * 2  # let EMA/RSI settle before trusting a signal


def detect_signal(df):
    """Look at the last two closed candles for a fresh EMA crossover."""
    if len(df) < MIN_BARS:
        return None
    prev, curr = df.iloc[-2], df.iloc[-1]

    crossed_up = prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
    crossed_down = prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]

    if crossed_up and RSI_MIN < curr["rsi"] < RSI_MAX:
        return "BUY"
    if crossed_down:
        return "SELL"
    return None


def send_discord_alert(signal, price, rsi):
    emoji = "\U0001F7E2" if signal == "BUY" else "\U0001F534"
    content = (
        f"{emoji} **{signal}** {TICKER} @ {price:.2f} "
        f"(EMA{FAST_EMA}/{SLOW_EMA} cross, RSI {rsi:.1f})"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def run_once():
    df = yf.download(TICKER, period=LOOKBACK, interval=INTERVAL, progress=False)
    if df.empty:
        print(f"[{TICKER}] no data returned this cycle, skipping")
        return

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = compute_indicators(df)
    signal = detect_signal(df)
    last_signal = load_last_signal()

    if signal and signal != last_signal:
        latest = df.iloc[-1]
        send_discord_alert(signal, latest["Close"], latest["rsi"])
        save_last_signal(signal)
        print(f"[{TICKER}] sent {signal} alert @ {latest['Close']:.2f}")
    else:
        print(f"[{TICKER}] no new signal (last sent: {last_signal})")


if __name__ == "__main__":
    print(
        f"Watching {TICKER} on {INTERVAL} candles | "
        f"EMA{FAST_EMA}/{SLOW_EMA} crossover | RSI filter {RSI_MIN}-{RSI_MAX} | "
        f"polling every {POLL_SECONDS}s"
    )
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[{TICKER}] error this cycle: {exc}")
        time.sleep(POLL_SECONDS)
