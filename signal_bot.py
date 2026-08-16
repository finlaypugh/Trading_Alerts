#!/usr/bin/env python3
"""
Standalone trading signal bot — BTC only, dual-strategy confirmation.

Polls BTC-USD price data on a schedule and only fires an alert when TWO
independent strategies agree on direction:

  Strategy 1 (trend):     EMA9/EMA21 crossover, filtered by RSI band
  Strategy 2 (momentum):  MACD line/signal crossover with a positive/
                           negative histogram in the same direction

If BOTH strategies agree, a STRONG alert fires. If only ONE of the two
agrees, a WEAK alert still fires (rather than being suppressed) so you
don't miss a potential move -- it's just clearly labeled as unconfirmed,
and the alert names exactly which strategy triggered it. If the two
strategies point in opposite directions, nothing fires (contradictory
signals aren't actionable either way).

The alert also includes a suggested stop-loss and take-profit, derived
from ATR (volatility) and scaled by signal strength (stronger agreement
= wider take-profit target, since a higher-confidence move is given more
room to run).

This script never places trades — it only sends notifications for you to
act on manually.

Setup:
    pip install yfinance pandas requests
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python signal_bot.py

Config is via environment variables (see the block below).
"""

import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ---- Config (override via environment variables) ----
TICKER = os.environ.get("SIGNAL_TICKER", "BTC-USD")          # Yahoo Finance ticker symbol
INTERVAL = os.environ.get("SIGNAL_INTERVAL", "15m")          # 1m,5m,15m,1h,1d ...
LOOKBACK = os.environ.get("SIGNAL_LOOKBACK", "5d")           # history window to pull each poll

# Strategy 1: EMA crossover + RSI filter
FAST_EMA = int(os.environ.get("SIGNAL_FAST_EMA", 9))
SLOW_EMA = int(os.environ.get("SIGNAL_SLOW_EMA", 21))
RSI_LEN = int(os.environ.get("SIGNAL_RSI_LEN", 14))
RSI_MIN = float(os.environ.get("SIGNAL_RSI_MIN", 40))
RSI_MAX = float(os.environ.get("SIGNAL_RSI_MAX", 70))

# Strategy 2: MACD crossover
MACD_FAST = int(os.environ.get("SIGNAL_MACD_FAST", 12))
MACD_SLOW = int(os.environ.get("SIGNAL_MACD_SLOW", 26))
MACD_SIGNAL = int(os.environ.get("SIGNAL_MACD_SIGNAL", 9))

# Volatility / risk management
ATR_LEN = int(os.environ.get("SIGNAL_ATR_LEN", 14))
SL_ATR_MULT = float(os.environ.get("SIGNAL_SL_ATR_MULT", 1.5))   # stop-loss distance = this * ATR
TP_RR_MIN = float(os.environ.get("SIGNAL_TP_RR_MIN", 1.5))       # reward:risk for weakest qualifying signal
TP_RR_MAX = float(os.environ.get("SIGNAL_TP_RR_MAX", 3.0))       # reward:risk for strongest signal

POLL_SECONDS = int(os.environ.get("SIGNAL_POLL_SECONDS", 900))   # 15 min default
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]          # required, no default

# Display names used in alerts to say exactly which strategy fired.
STRAT1_NAME = f"EMA{FAST_EMA}/{SLOW_EMA}+RSI{RSI_LEN}"
STRAT2_NAME = f"MACD{MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}"

# Weak (single-strategy) signals are always scored below this ceiling so
# they can never read as more confident than a fully-confirmed signal.
WEAK_STRENGTH_CAP = float(os.environ.get("SIGNAL_WEAK_STRENGTH_CAP", 0.5))

STATE_FILE = Path(__file__).parent / f".state_{TICKER.replace('/', '_')}.json"


def load_last_signal():
    """Return the last sent signal (e.g. "BUY"/"SELL") or None if missing/corrupt.

    Historically this module exposed a simple single-value persistence API
    used in tests and in run-time. The previous implementation returned a
    tuple and required a separate "confirmed" field; that made callers and
    tests diverge. Use a single-value API for backwards compatibility with
    the test-suite: save_last_signal(value) / load_last_signal() -> value|None.
    """
    try:
        text = STATE_FILE.read_text()
    except (FileNotFoundError, OSError):
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def save_last_signal(signal):
    """Persist a simple last-signal value (string or JSON-serializable object).

    Overwrites any previous value in STATE_FILE.
    """
    STATE_FILE.write_text(json.dumps(signal))


def compute_indicators(df):
    df = df.copy()

    # --- Strategy 1 inputs: EMA crossover + RSI ---
    df["ema_fast"] = df["Close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=SLOW_EMA, adjust=False).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # --- Strategy 2 inputs: MACD ---
    ema_macd_fast = df["Close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_macd_slow = df["Close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"] = ema_macd_fast - ema_macd_slow
    df["macd_signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # --- Volatility: ATR (Wilder's) ---
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_LEN, adjust=False).mean()

    return df


MIN_BARS = max(SLOW_EMA, RSI_LEN, MACD_SLOW + MACD_SIGNAL, ATR_LEN) * 2  # let everything settle


def _clip01(x):
    return max(0.0, min(1.0, x))


def detect_signal(df):
    """
    Require BOTH strategies to agree on direction using the last two closed
    candles. Returns (signal, strength) where strength is 0-1 (0 = just
    barely qualifies, 1 = maximally strong agreement), or (None, 0).
    """
    if len(df) < MIN_BARS:
        return None, 0.0

    prev, curr = df.iloc[-2], df.iloc[-1]

    # Strategy 1: EMA crossover + RSI band
    ema_crossed_up = prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
    ema_crossed_down = prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]
    rsi_ok_buy = RSI_MIN < curr["rsi"] < RSI_MAX
    rsi_ok_sell = curr["rsi"] < (100 - RSI_MIN)  # symmetric-ish guard against selling into deep oversold

    strat1_buy = ema_crossed_up and rsi_ok_buy
    strat1_sell = ema_crossed_down and rsi_ok_sell

    # Strategy 2: MACD line/signal crossover, histogram confirms direction
    macd_crossed_up = prev["macd"] <= prev["macd_signal"] and curr["macd"] > curr["macd_signal"]
    macd_crossed_down = prev["macd"] >= prev["macd_signal"] and curr["macd"] < curr["macd_signal"]

    strat2_buy = macd_crossed_up and curr["macd_hist"] > 0
    strat2_sell = macd_crossed_down and curr["macd_hist"] < 0

    signal = None
    if strat1_buy and strat2_buy:
        signal = "BUY"
    elif strat1_sell and strat2_sell:
        signal = "SELL"

    if signal is None:
        return None, 0.0

    # --- Strength score: how convincingly both strategies agree ---
    # RSI component: distance from the band's edges (more central = stronger)
    rsi_mid = (RSI_MIN + RSI_MAX) / 2
    rsi_half_width = (RSI_MAX - RSI_MIN) / 2
    rsi_score = _clip01(1 - abs(curr["rsi"] - rsi_mid) / rsi_half_width) if rsi_half_width else 0.5

    # MACD component: histogram size relative to its recent volatility
    recent_hist_std = df["macd_hist"].tail(50).std()
    macd_score = _clip01(abs(curr["macd_hist"]) / (2 * recent_hist_std)) if recent_hist_std else 0.5

    # EMA separation component: gap between EMAs relative to ATR (bigger = more decisive cross)
    ema_gap = abs(curr["ema_fast"] - curr["ema_slow"])
    ema_score = _clip01(ema_gap / (curr["atr"])) if curr["atr"] else 0.5

    strength = (rsi_score + macd_score + ema_score) / 3
    return signal, strength


def build_sl_tp(signal, price, atr, strength):
    """Stop distance is fixed by volatility (ATR); the take-profit target
    scales with signal strength, so higher-confidence signals get more
    room to run rather than a tighter stop."""
    sl_distance = SL_ATR_MULT * atr
    rr = TP_RR_MIN + (TP_RR_MAX - TP_RR_MIN) * strength
    tp_distance = sl_distance * rr

    if signal == "BUY":
        sl = price - sl_distance
        tp = price + tp_distance
    else:
        sl = price + sl_distance
        tp = price - tp_distance
    return sl, tp, rr


def send_discord_alert(signal, price, rsi, atr, strength, sl, tp, rr):
    emoji = "\U0001F7E2" if signal == "BUY" else "\U0001F534"
    stars = "\u2B50" * max(1, round(strength * 5))
    content = (
        f"{emoji} **{signal}** {TICKER} @ {price:,.2f}\n"
        f"Confirmed by EMA{FAST_EMA}/{SLOW_EMA}+RSI and MACD{MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL} "
        f"(RSI {rsi:.1f})\n"
        f"Signal strength: {strength * 100:.0f}% {stars}\n"
        f"SL: {sl:,.2f}  |  TP: {tp:,.2f}  |  R:R \u2248 1:{rr:.2f}\n"
        f"_Not financial advice — informational only, act at your own discretion._"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def send_startup_message():
    content = (
        f"\u2705 **Signal bot started** — watching {TICKER} on {INTERVAL} candles\n"
        f"Strategy 1: EMA{FAST_EMA}/{SLOW_EMA} + RSI{RSI_LEN} ({RSI_MIN}-{RSI_MAX}) | "
        f"Strategy 2: MACD {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL} | both must agree\n"
        f"Polling every {POLL_SECONDS}s"
    )
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[{TICKER}] failed to send startup message: {exc}")


def run_once():
    df = yf.download(TICKER, period=LOOKBACK, interval=INTERVAL, progress=False)
    if df.empty:
        print(f"[{TICKER}] no data returned this cycle, skipping")
        return

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = compute_indicators(df)
    signal, strength = detect_signal(df)
    last_signal = load_last_signal()

    if signal and signal != last_signal:
        latest = df.iloc[-1]
        sl, tp, rr = build_sl_tp(signal, latest["Close"], latest["atr"], strength)
        send_discord_alert(signal, latest["Close"], latest["rsi"], latest["atr"], strength, sl, tp, rr)
        save_last_signal(signal)
        print(f"[{TICKER}] sent {signal} alert @ {latest['Close']:.2f} (strength {strength:.2f})")
    else:
        print(f"[{TICKER}] no new confirmed signal (last sent: {last_signal})")


if __name__ == "__main__":
    print(
        f"Watching {TICKER} on {INTERVAL} candles | "
        f"Strategy 1: EMA{FAST_EMA}/{SLOW_EMA} + RSI{RSI_LEN} ({RSI_MIN}-{RSI_MAX}) | "
        f"Strategy 2: MACD {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL} | "
        f"both required to agree | polling every {POLL_SECONDS}s"
    )
    send_startup_message()
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[{TICKER}] error this cycle: {exc}")
        time.sleep(POLL_SECONDS)
