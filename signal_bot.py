#!/usr/bin/env python3
"""
Standalone trading signal bot — BTC only, dual-strategy confirmation.

Polls BTC-USD price data on a schedule and combines two independent
strategies: one supplies the entry *event*, the other supplies the market
*regime* that event has to happen in.

  Strategy 1 (trigger):  Williams Fractals breakout. A fractal is a swing
                         pivot confirmed n bars after it forms. Fractals
                         alone are not directional, so the tradable event
                         is price closing through the most recently
                         confirmed pivot: above the last up-fractal high
                         (buy) or below the last down-fractal low (sell).

  Strategy 2 (regime):   Triple EMA 50/100/200 stack. Bullish when
                         ema1 > ema2 > ema3 and price is above ema1;
                         bearish when the stack is inverted and price is
                         below ema1; neutral otherwise.

The two are deliberately asymmetric. Requiring both to *cross* on the same
candle would fire approximately never — a 200-period EMA stack flips
direction a handful of times a month. So the fractal break is the trigger
and the EMA stack is the confirming state:

  break + matching regime   -> STRONG alert
  break + neutral regime    -> WEAK alert (capped strength, clearly labeled)
  break + opposing regime   -> nothing (a contradiction is not actionable)
  no break                  -> nothing (regime alone gives no entry bar)

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

import pandas as pd
import requests
import yfinance as yf

# ---- Config (override via environment variables) ----
TICKER = os.environ.get("SIGNAL_TICKER", "BTC-USD")          # Yahoo Finance ticker symbol
INTERVAL = os.environ.get("SIGNAL_INTERVAL", "5m")           # 1m,5m,15m,1h,1d ...
LOOKBACK = os.environ.get("SIGNAL_LOOKBACK", "5d")           # history window to pull each poll

# Strategy 1: Williams Fractals
FRACTAL_N = int(os.environ.get("SIGNAL_FRACTAL_N", 2))       # bars either side of the pivot
FRACTAL_MAX_PLATEAU = int(os.environ.get("SIGNAL_FRACTAL_MAX_PLATEAU", 4))  # equal-high run allowed on the left

if FRACTAL_N < 2:
    raise ValueError(
        f"SIGNAL_FRACTAL_N must be >= 2 (got {FRACTAL_N}); "
        f"a fractal needs at least two bars either side of the pivot"
    )
if FRACTAL_MAX_PLATEAU < 0:
    raise ValueError(f"SIGNAL_FRACTAL_MAX_PLATEAU must be >= 0 (got {FRACTAL_MAX_PLATEAU})")

# Strategy 2: Triple EMA
EMA_1 = int(os.environ.get("SIGNAL_EMA_1", 50))
EMA_2 = int(os.environ.get("SIGNAL_EMA_2", 100))
EMA_3 = int(os.environ.get("SIGNAL_EMA_3", 200))

# Volatility / risk management
ATR_LEN = int(os.environ.get("SIGNAL_ATR_LEN", 14))
SL_ATR_MULT = float(os.environ.get("SIGNAL_SL_ATR_MULT", 1.5))   # stop-loss distance = this * ATR
TP_RR_MIN = float(os.environ.get("SIGNAL_TP_RR_MIN", 1.5))       # reward:risk for weakest qualifying signal
TP_RR_MAX = float(os.environ.get("SIGNAL_TP_RR_MAX", 3.0))       # reward:risk for strongest signal

POLL_SECONDS = int(os.environ.get("SIGNAL_POLL_SECONDS", 900))   # 15 min default
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]          # required, no default

# Display names used in alerts to say exactly which strategy fired.
STRAT1_NAME = f"Fractals(n={FRACTAL_N})"
STRAT2_NAME = f"TEMA{EMA_1}/{EMA_2}/{EMA_3}"

# Weak (single-strategy) signals are always scored below this ceiling so
# they can never read as more confident than a fully-confirmed signal.
WEAK_STRENGTH_CAP = float(os.environ.get("SIGNAL_WEAK_STRENGTH_CAP", 0.5))

STATE_FILE = Path(__file__).parent / f".state_{TICKER.replace('/', '_')}.json"

# Enough history for the slowest EMA to settle, plus a full fractal window
# (n bars either side of a pivot, plus the n-bar confirmation delay).
MIN_BARS = max(EMA_3 * 2, ATR_LEN * 2, (2 * FRACTAL_N) + 1 + FRACTAL_N)


def load_last_signal():
    """Return the last alert as {"signal": ..., "tier": ...}, or None."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Older versions stored a bare direction string. Treat those as STRONG so
    # an existing state file cannot replay as a spurious WEAK->STRONG upgrade.
    if isinstance(data, str):
        return {"signal": data, "tier": "STRONG"}
    if isinstance(data, dict) and data.get("signal"):
        return {"signal": data["signal"], "tier": data.get("tier", "STRONG")}
    return None


def save_last_signal(signal, tier):
    STATE_FILE.write_text(json.dumps({"signal": signal, "tier": tier}))


def _fractal_mask(series, n, max_plateau, up=True):
    """
    Boolean Series: True on bars that are a Williams fractal pivot.

    Port of the Pine implementation. In Pine, high[k] is k bars *ago*, so
    for a pivot candidate high[n]: high[n-i] are the newer bars to its
    right and high[n+i] the older bars to its left. The right side must be
    strictly beaten by the pivot; the left side allows a run of up to
    max_plateau equal bars immediately adjacent before the strict
    requirement kicks in, which is what lets a flat double top still count.
    """
    cmp_strict = (lambda a, b: a < b) if up else (lambda a, b: a > b)
    cmp_eq_ok = (lambda a, b: a <= b) if up else (lambda a, b: a >= b)

    # Right side (newer bars, p+1 .. p+n): all strictly beaten by the pivot.
    right = pd.Series(True, index=series.index)
    for i in range(1, n + 1):
        right &= cmp_strict(series.shift(-i), series)

    # Left side (older bars): try every plateau length and OR them together.
    left = pd.Series(False, index=series.index)
    for plateau in range(0, max_plateau + 1):
        variant = pd.Series(True, index=series.index)
        for j in range(1, plateau + 1):
            variant &= cmp_eq_ok(series.shift(j), series)
        for i in range(1, n + 1):
            variant &= cmp_strict(series.shift(i + plateau), series)
        left |= variant

    return (right & left).fillna(False).astype(bool)


def compute_indicators(df):
    df = df.copy()

    # --- Strategy 2 inputs: triple EMA stack ---
    df["ema_1"] = df["Close"].ewm(span=EMA_1, adjust=False).mean()
    df["ema_2"] = df["Close"].ewm(span=EMA_2, adjust=False).mean()
    df["ema_3"] = df["Close"].ewm(span=EMA_3, adjust=False).mean()

    # --- Strategy 1 inputs: Williams fractals ---
    up_pivot = _fractal_mask(df["High"], FRACTAL_N, FRACTAL_MAX_PLATEAU, up=True)
    down_pivot = _fractal_mask(df["Low"], FRACTAL_N, FRACTAL_MAX_PLATEAU, up=False)
    df["up_fractal"] = up_pivot
    df["down_fractal"] = down_pivot

    # The .shift(FRACTAL_N) below is the single most important line in this
    # file. A pivot at bar p is only *knowable* at bar p + n, because the n
    # bars to its right have to close first. Forward-filling the raw pivot
    # would let the bot read levels that could not have existed at the time:
    # it backtests beautifully and performs badly live. Shift first, ffill
    # second.
    df["fractal_high"] = df["High"].where(up_pivot).shift(FRACTAL_N).ffill()
    df["fractal_low"] = df["Low"].where(down_pivot).shift(FRACTAL_N).ffill()

    # --- Volatility: ATR (Wilder smoothing) ---
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


def _clip01(x):
    return max(0.0, min(1.0, x))


def detect_signal(df):
    """
    Fractal breakout supplies the trigger; the EMA stack supplies the
    regime it has to happen in. Returns (signal, strength, tier, reason)
    where strength is 0-1 and tier is "STRONG"/"WEAK", or
    (None, 0.0, None, "") when nothing is actionable.
    """
    if len(df) < MIN_BARS:
        return None, 0.0, None, ""

    prev, curr = df.iloc[-2], df.iloc[-1]

    # --- Strategy 1: fractal breakout ---
    # Levels are read off the *previous* bar, so the break is measured
    # against a level that was already known before this candle opened.
    level_hi = prev["fractal_high"]
    level_lo = prev["fractal_low"]

    frac_buy = pd.notna(level_hi) and prev["Close"] <= level_hi and curr["Close"] > level_hi
    frac_sell = pd.notna(level_lo) and prev["Close"] >= level_lo and curr["Close"] < level_lo

    if not frac_buy and not frac_sell:
        return None, 0.0, None, ""
    if frac_buy and frac_sell:
        # A bar straddling both levels tells us nothing directional. Not
        # reachable with close-based triggers, but it would be if the trigger
        # were ever switched to intrabar High/Low, so guard it here.
        return None, 0.0, None, ""

    # --- Strategy 2: EMA regime ---
    bull = curr["ema_1"] > curr["ema_2"] > curr["ema_3"] and curr["Close"] > curr["ema_1"]
    bear = curr["ema_1"] < curr["ema_2"] < curr["ema_3"] and curr["Close"] < curr["ema_1"]

    if (frac_buy and bear) or (frac_sell and bull):
        return None, 0.0, None, ""

    signal = "BUY" if frac_buy else "SELL"
    level = level_hi if frac_buy else level_lo
    side = "above" if frac_buy else "below"

    # --- Strength score: how decisive the break and the regime are ---
    atr = curr["atr"]
    atr_ok = pd.notna(atr) and atr > 0

    # How far the close pushed past the broken level, in ATRs.
    break_score = _clip01(abs(curr["Close"] - level) / atr) if atr_ok else 0.5

    # How widely fanned the EMA stack is (a tight stack is an indecisive one).
    stack_score = _clip01(abs(curr["ema_1"] - curr["ema_3"]) / (3 * atr)) if atr_ok else 0.5

    # Momentum of the fast EMA over the last 10 bars.
    prior_ema1 = df["ema_1"].iloc[-11]
    if atr_ok and pd.notna(prior_ema1):
        slope_score = _clip01(abs(curr["ema_1"] - prior_ema1) / atr)
    else:
        slope_score = 0.5

    strength = (break_score + stack_score + slope_score) / 3

    if (frac_buy and bull) or (frac_sell and bear):
        direction = "bullish" if frac_buy else "bearish"
        reason = f"{STRAT1_NAME} break {side} {level:,.2f} + {STRAT2_NAME} {direction} stack"
        return signal, strength, "STRONG", reason

    reason = f"{STRAT1_NAME} break {side} {level:,.2f}, {STRAT2_NAME} neutral (unconfirmed)"
    return signal, min(strength, WEAK_STRENGTH_CAP), "WEAK", reason


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


def send_discord_alert(signal, price, atr, strength, tier, reason, sl, tp, rr):
    emoji = "\U0001F7E2" if signal == "BUY" else "\U0001F534"
    stars = "⭐" * max(1, round(strength * 5))
    content = (
        f"{emoji} **{tier} {signal}** {TICKER} @ {price:,.2f}\n"
        f"{reason}\n"
        f"Signal strength: {strength * 100:.0f}% {stars}\n"
        f"SL: {sl:,.2f}  |  TP: {tp:,.2f}  |  R:R ≈ 1:{rr:.2f}\n"
        f"_Not financial advice — informational only, act at your own discretion._"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def send_startup_message():
    content = (
        f"✅ **Signal bot started** — watching {TICKER} on {INTERVAL} candles\n"
        f"Strategy 1: Williams Fractals (n={FRACTAL_N}) breakout\n"
        f"Strategy 2: Triple EMA {EMA_1}/{EMA_2}/{EMA_3} regime\n"
        f"Both agree = STRONG, one = WEAK, conflict = silent\n"
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

    if len(df) < MIN_BARS:
        print(
            f"[{TICKER}] only {len(df)} bars returned, need {MIN_BARS} to warm up EMA{EMA_3} - "
            f"widen SIGNAL_LOOKBACK (currently {LOOKBACK}) or use a coarser SIGNAL_INTERVAL"
        )
        return

    df = compute_indicators(df)
    signal, strength, tier, reason = detect_signal(df)

    last = load_last_signal()
    last_signal = last["signal"] if last else None
    last_tier = last["tier"] if last else None

    is_new_direction = signal is not None and signal != last_signal
    is_upgrade = (
        signal is not None
        and signal == last_signal
        and last_tier == "WEAK"
        and tier == "STRONG"
    )

    if is_new_direction or is_upgrade:
        latest = df.iloc[-1]
        sl, tp, rr = build_sl_tp(signal, latest["Close"], latest["atr"], strength)
        send_discord_alert(signal, latest["Close"], latest["atr"], strength, tier, reason, sl, tp, rr)
        save_last_signal(signal, tier)
        print(f"[{TICKER}] sent {tier} {signal} alert @ {latest['Close']:.2f} (strength {strength:.2f})")
    else:
        print(f"[{TICKER}] no new confirmed signal (last sent: {last_tier} {last_signal})")


if __name__ == "__main__":
    print(
        f"Watching {TICKER} on {INTERVAL} candles | "
        f"Strategy 1: Williams Fractals (n={FRACTAL_N}) breakout | "
        f"Strategy 2: Triple EMA {EMA_1}/{EMA_2}/{EMA_3} regime | "
        f"both agree = STRONG, one = WEAK, conflict = silent | "
        f"polling every {POLL_SECONDS}s"
    )
    send_startup_message()
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[{TICKER}] error this cycle: {exc}")
        time.sleep(POLL_SECONDS)
