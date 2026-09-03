#!/usr/bin/env python3
"""
Standalone trading signal bot — fractal pullback entries in a stacked EMA trend.

This is one setup, not two strategies voting. A triple-EMA stack supplies the
trend filter; a Williams Fractal supplies the timing trigger. Both are required.

  Long:   ema_fast > ema_mid > ema_slow and has been for MIN_STACK_BARS
          closed bars, price has pulled back to close below the fast (depth 1)
          or mid (depth 2) EMA, a green arrow (swing low) whose pivot sits
          inside that pullback then confirms, and price has not closed below
          the slow EMA at any point during the episode.

  Short:  the exact mirror — inverted stack, a close above the fast or mid
          EMA, a red arrow (swing high), no close above the slow EMA.

Anything else is the third state the source names explicitly: no trade. A
stack that has only just uncrossed is disorderly, not aligned, and a fractal
that formed before its pullback began is not the reversal of that pullback.

Arrow convention: green marks a swing LOW (a down fractal) and is the long
trigger; red marks a swing HIGH (an up fractal) and is the short trigger.
Inverting this inverts every signal the bot produces, so it has its own test.

Stops reference the EMA one step beyond the deepest one price breached — a
shallow pullback to the fast EMA stops below the mid, a deeper one to the mid
stops below the slow — offset by SL_BUFFER_ATR × ATR so the stop is not
sitting exactly on a line that price routinely wicks. Targets are a fixed
RR multiple of that risk.

Three things this bot has to get right that a naive implementation does not:

  * A fractal at bar p is only knowable at bar p + n. TradingView draws the
    arrow back at the pivot, which makes chart examples look like entries
    happened n bars earlier than any live bot could manage. Every arrow is
    shifted forward by n.

  * yfinance returns the in-progress candle as the last row. All indicator
    and signal logic runs on closed bars only.

  * Gold futures have a daily settlement break and weekend gaps. Pivots whose
    window spans a break are discarded and pullback episodes reset across one,
    otherwise the first bar after the weekend reliably fakes a pivot.

This script never places trades — it only sends notifications for you to act
on manually.

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
TICKER = os.environ.get("SIGNAL_TICKER", "")                 # Yahoo Finance ticker symbol
INTERVAL = os.environ.get("SIGNAL_INTERVAL", "15m")          # 1m,5m,15m,1h,1d ...
LOOKBACK = os.environ.get("SIGNAL_LOOKBACK", "10d")          # history window to pull each poll

# Trend filter: triple EMA
EMA_FAST = int(os.environ.get("SIGNAL_EMA_FAST", 20))
EMA_MID = int(os.environ.get("SIGNAL_EMA_MID", 50))
EMA_SLOW = int(os.environ.get("SIGNAL_EMA_SLOW", 100))

# Trigger: Williams Fractals
FRACTAL_N = int(os.environ.get("SIGNAL_FRACTAL_N", 2))
FRACTAL_MAX_PLATEAU = int(os.environ.get("SIGNAL_FRACTAL_MAX_PLATEAU", 4))

# Pullback episode handling
PULLBACK_EXPIRY_BARS = int(os.environ.get("SIGNAL_PULLBACK_EXPIRY_BARS", 3))
REQUIRE_PULLBACK = os.environ.get("SIGNAL_REQUIRE_PULLBACK", "true").lower() == "true"

# The fractal has to sit inside the pullback it is supposed to be reversing.
# Without this a pivot that formed before the pullback even started still
# fires, as long as a pullback happens within the next n bars.
REQUIRE_PIVOT_IN_PULLBACK = (
    os.environ.get("SIGNAL_REQUIRE_PIVOT_IN_PULLBACK", "true").lower() == "true"
)

# The "no trade" state: MAs crossing or disorderly. A stack is only ordered
# once it has *stayed* ordered, so require this many consecutive closed bars.
MIN_STACK_BARS = int(os.environ.get("SIGNAL_MIN_STACK_BARS", 3))

# 2 mirrors the long side. 1 restricts shorts to the single pullback-above-fast
# entry the source material actually describes.
SHORT_MAX_DEPTH = int(os.environ.get("SIGNAL_SHORT_MAX_DEPTH", 2))

# Risk
RR = float(os.environ.get("SIGNAL_RR", 1.5))
SL_BUFFER_ATR = float(os.environ.get("SIGNAL_SL_BUFFER_ATR", 0.25))
MIN_STACK_SEP_ATR = float(os.environ.get("SIGNAL_MIN_STACK_SEP_ATR", 0.0))
MAX_RISK_ATR = float(os.environ.get("SIGNAL_MAX_RISK_ATR", 0.0))  # 0 = no ceiling
ATR_LEN = int(os.environ.get("SIGNAL_ATR_LEN", 14))

# Candle hygiene
DROP_UNCLOSED_BAR = os.environ.get("SIGNAL_DROP_UNCLOSED_BAR", "true").lower() == "true"
SESSION_GAP_MULT = float(os.environ.get("SIGNAL_SESSION_GAP_MULT", 2.0))

# Alert throttling
COOLDOWN_BARS = int(os.environ.get("SIGNAL_COOLDOWN_BARS", 4))

POLL_SECONDS = int(os.environ.get("SIGNAL_POLL_SECONDS", 300))
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]      # required, no default

if FRACTAL_N < 2:
    raise ValueError(
        f"SIGNAL_FRACTAL_N must be >= 2 (got {FRACTAL_N}); "
        f"a fractal needs at least two bars either side of the pivot"
    )
if FRACTAL_MAX_PLATEAU < 0:
    raise ValueError(f"SIGNAL_FRACTAL_MAX_PLATEAU must be >= 0 (got {FRACTAL_MAX_PLATEAU})")
if not (EMA_FAST < EMA_MID < EMA_SLOW):
    raise ValueError(
        f"EMA lengths must satisfy fast < mid < slow "
        f"(got {EMA_FAST}/{EMA_MID}/{EMA_SLOW})"
    )

# Display names used in alerts to say exactly which strategy fired.
STRAT1_NAME = f"Fractals(n={FRACTAL_N})"
STRAT2_NAME = f"EMA{EMA_FAST}/{EMA_MID}/{EMA_SLOW}"

# Weak (no-pullback) signals are always scored below this ceiling so they can
# never read as more confident than a fully-qualified setup.
WEAK_STRENGTH_CAP = float(os.environ.get("SIGNAL_WEAK_STRENGTH_CAP", 0.5))

STATE_FILE = Path(__file__).parent / f".state_{TICKER.replace('/', '_')}.json"

# Enough history for the slowest EMA to settle, plus a full fractal window
# (n bars either side of a pivot, plus the n-bar confirmation delay).
MIN_BARS = max(EMA_SLOW * 2, ATR_LEN * 2, (2 * FRACTAL_N) + 1 + FRACTAL_N)


def interval_minutes(interval=None):
    """'15m' -> 15, '1h' -> 60, '1d' -> 1440."""
    if interval is None:
        interval = INTERVAL
    text = str(interval).strip().lower()
    units = {"m": 1, "h": 60, "d": 1440}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise ValueError(
            f"cannot parse interval {interval!r}; expected a number followed by m, h or d"
        )
    return int(text[:-1]) * units[text[-1]]


def load_last_signal():
    """Return the last alert as a dict, or None if there is no usable state."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Older versions stored a bare direction string. Treat those as STRONG so
    # an existing state file cannot replay as a spurious WEAK->STRONG upgrade.
    if isinstance(data, str):
        return {"signal": data, "tier": "STRONG", "depth": 1, "bar_time": None}
    if isinstance(data, dict) and data.get("signal"):
        return {
            "signal": data["signal"],
            "tier": data.get("tier", "STRONG"),
            "depth": data.get("depth", 1),
            "bar_time": data.get("bar_time"),
        }
    return None


def save_last_signal(signal, tier, depth, bar_time):
    STATE_FILE.write_text(
        json.dumps(
            {
                "signal": signal,
                "tier": tier,
                "depth": int(depth),
                "bar_time": None if bar_time is None else str(bar_time),
            }
        )
    )


def _clip01(x):
    return max(0.0, min(1.0, x))


def _stack_open(fast, slow, atr):
    """Is the EMA fan actually separated, or are the lines tangled together?

    Off by default: 'not crossing each other' is a visual judgement the source
    material never quantifies. Raise MIN_STACK_SEP_ATR to require real width.
    """
    if MIN_STACK_SEP_ATR <= 0:
        return True
    if pd.isna(atr) or atr <= 0:
        return True
    return abs(fast - slow) >= MIN_STACK_SEP_ATR * atr


def drop_unclosed_bar(df):
    """
    yfinance returns the in-progress candle as the final row. Acting on it
    produces alerts for setups that may not exist by the candle's close, so
    drop it unless its close time has already passed.
    """
    if not DROP_UNCLOSED_BAR or df.empty:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        # Without timestamps we cannot tell, and acting on an unclosed bar is
        # the more expensive mistake. Losing one closed bar per poll is free.
        return df.iloc[:-1]

    last_open = df.index[-1]
    close_time = last_open + pd.Timedelta(minutes=interval_minutes())
    if df.index.tz is not None:
        now = pd.Timestamp.now(tz=df.index.tz)
    else:
        now = pd.Timestamp.now("UTC").tz_localize(None)

    if close_time > now:
        return df.iloc[:-1]
    return df


def mark_session_gaps(df):
    """
    True on the first bar after a break longer than SESSION_GAP_MULT
    intervals. Gold futures have a daily settlement break and weekends;
    a fractal spanning one is not a real pivot and a pullback episode
    should not survive it.
    """
    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex) or len(df) == 0:
        df["session_gap"] = False
        return df
    expected = pd.Timedelta(minutes=interval_minutes())
    delta = df.index.to_series().diff()
    df["session_gap"] = (delta > (expected * SESSION_GAP_MULT)).to_numpy()
    return df


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


def _clear_pivots_spanning_gaps(pivots, session_gap, n):
    """Drop pivots whose +/-n window contains a session break."""
    window = 2 * n + 1
    spans_gap = (
        session_gap.astype(float)
        .rolling(window, center=True, min_periods=1)
        .max()
        .fillna(0.0)
        > 0
    )
    return pivots & ~spans_gap


def compute_pullback_state(df):
    """
    Adds long_depth / short_depth (0/1/2), long_vetoed / short_vetoed and
    bull_stack_bars / bear_stack_bars.

      depth 0 = no qualifying pullback yet
      depth 1 = closed beyond the fast EMA
      depth 2 = closed beyond the mid EMA (deeper pullback -> wider stop)
      vetoed  = closed beyond the slow EMA this episode, so the setup is dead
      stack_bars = consecutive closed bars the stack has held its order for,
                   reset by a cross or a session break

    Written as an explicit bar-by-bar pass. Clarity beats vectorisation here,
    and a few hundred rows per poll costs nothing.
    """
    df = df.copy()
    n = len(df)

    close = df["Close"].to_numpy(dtype=float)
    fast = df["ema_fast"].to_numpy(dtype=float)
    mid = df["ema_mid"].to_numpy(dtype=float)
    slow = df["ema_slow"].to_numpy(dtype=float)
    atr = df["atr"].to_numpy(dtype=float)
    gap = df["session_gap"].to_numpy(dtype=bool)

    long_depth = [0] * n
    short_depth = [0] * n
    long_vetoed = [False] * n
    short_vetoed = [False] * n
    bull_stack_bars = [0] * n
    bear_stack_bars = [0] * n

    l_depth = s_depth = 0
    l_veto = s_veto = False
    l_bars_back = s_bars_back = 0
    l_stack_bars = s_stack_bars = 0

    for i in range(n):
        if gap[i]:
            l_depth = s_depth = 0
            l_veto = s_veto = False
            l_bars_back = s_bars_back = 0

        separated = _stack_open(fast[i], slow[i], atr[i])
        bull = fast[i] > mid[i] > slow[i] and separated
        bear = fast[i] < mid[i] < slow[i] and separated

        # How long the stack has held this order. `bull` is already true on the
        # very first bar after an uncross, which is the disorderly state the
        # setup is meant to sit out; a session break severs the run outright.
        l_stack_bars = l_stack_bars + 1 if bull and not gap[i] else 0
        s_stack_bars = s_stack_bars + 1 if bear and not gap[i] else 0

        # --- long side ---
        if not bull:
            l_depth = 0
            l_veto = False
            l_bars_back = 0
        else:
            if close[i] < slow[i]:
                l_veto = True
            if close[i] < mid[i]:
                l_depth = max(l_depth, 2)
            elif close[i] < fast[i]:
                l_depth = max(l_depth, 1)

            if close[i] > fast[i]:
                l_bars_back += 1
                if l_bars_back >= PULLBACK_EXPIRY_BARS:
                    l_depth = 0
                    l_veto = False
            else:
                l_bars_back = 0

        # --- short side (mirror) ---
        if not bear:
            s_depth = 0
            s_veto = False
            s_bars_back = 0
        else:
            if close[i] > slow[i]:
                s_veto = True
            if close[i] > mid[i]:
                s_depth = max(s_depth, 2)
            elif close[i] > fast[i]:
                s_depth = max(s_depth, 1)

            if close[i] < fast[i]:
                s_bars_back += 1
                if s_bars_back >= PULLBACK_EXPIRY_BARS:
                    s_depth = 0
                    s_veto = False
            else:
                s_bars_back = 0

        long_depth[i] = l_depth
        short_depth[i] = s_depth
        long_vetoed[i] = l_veto
        short_vetoed[i] = s_veto
        bull_stack_bars[i] = l_stack_bars
        bear_stack_bars[i] = s_stack_bars

    df["long_depth"] = long_depth
    df["short_depth"] = short_depth
    df["long_vetoed"] = long_vetoed
    df["short_vetoed"] = short_vetoed
    df["bull_stack_bars"] = bull_stack_bars
    df["bear_stack_bars"] = bear_stack_bars
    return df


def compute_indicators(df):
    df = df.copy()

    # --- Trend filter: triple EMA stack ---
    df["ema_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_mid"] = df["Close"].ewm(span=EMA_MID, adjust=False).mean()
    df["ema_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()

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

    # Session breaks have to be known before the fractals, since a pivot whose
    # window straddles one is an artefact of the gap rather than a real swing.
    df = mark_session_gaps(df)

    # --- Pullback episodes ---
    # Computed before the fractals because a pivot has to be scored against the
    # pullback state that existed at the pivot bar itself. Depends only on
    # Close, the EMAs, ATR and session_gap, all of which are already here.
    df = compute_pullback_state(df)

    # --- Trigger: Williams fractals ---
    up_pivot = _fractal_mask(df["High"], FRACTAL_N, FRACTAL_MAX_PLATEAU, up=True)
    down_pivot = _fractal_mask(df["Low"], FRACTAL_N, FRACTAL_MAX_PLATEAU, up=False)
    up_pivot = _clear_pivots_spanning_gaps(up_pivot, df["session_gap"], FRACTAL_N)
    down_pivot = _clear_pivots_spanning_gaps(down_pivot, df["session_gap"], FRACTAL_N)
    df["up_fractal"] = up_pivot
    df["down_fractal"] = down_pivot

    # The .shift(FRACTAL_N) below is the single most important line in this
    # file. A pivot at bar p is only *knowable* at bar p + n, because the n
    # bars to its right have to close first. TradingView draws the arrow back
    # at the pivot, so chart examples look like they entered n bars earlier
    # than any live bot could. Reading the pivot bar directly backtests
    # beautifully and performs badly live.
    #
    # Green marks a swing LOW and is the long trigger; red marks a swing HIGH
    # and is the short trigger. Swapping these inverts every signal.
    df["green_arrow"] = down_pivot.shift(FRACTAL_N).fillna(False).astype(bool)
    df["red_arrow"] = up_pivot.shift(FRACTAL_N).fillna(False).astype(bool)

    # The pullback depth that was live at the *pivot* bar, carried to the
    # confirmation bar by the same shift the arrows use, so no lookahead is
    # introduced. Zero means the fractal formed outside a pullback.
    df["long_pivot_depth"] = (
        df["long_depth"].where(down_pivot, 0).shift(FRACTAL_N).fillna(0).astype(int)
    )
    df["short_pivot_depth"] = (
        df["short_depth"].where(up_pivot, 0).shift(FRACTAL_N).fillna(0).astype(int)
    )

    return df


def detect_signal(df):
    """
    Returns (signal, strength, tier, depth, reason), or
    (None, 0.0, None, 0, "") when nothing is actionable.
    """
    if len(df) < MIN_BARS:
        return None, 0.0, None, 0, ""

    curr = df.iloc[-1]

    separated = _stack_open(curr["ema_fast"], curr["ema_slow"], curr["atr"])
    bull = curr["ema_fast"] > curr["ema_mid"] > curr["ema_slow"] and separated
    bear = curr["ema_fast"] < curr["ema_mid"] < curr["ema_slow"] and separated

    # A stack that only just uncrossed is the source's third state -- no trade
    # -- so alignment has to have survived MIN_STACK_BARS closed bars.
    bull_settled = bull and int(curr["bull_stack_bars"]) >= MIN_STACK_BARS
    bear_settled = bear and int(curr["bear_stack_bars"]) >= MIN_STACK_BARS

    long_ok = bool(curr["green_arrow"]) and bull_settled and not bool(curr["long_vetoed"])
    short_ok = bool(curr["red_arrow"]) and bear_settled and not bool(curr["short_vetoed"])

    if long_ok and short_ok:
        # The stack cannot be both bull and bear, so this is a bug if it fires.
        print(f"[{TICKER}] BUG: both long and short qualified on the same bar")
        return None, 0.0, None, 0, ""
    if not long_ok and not short_ok:
        return None, 0.0, None, 0, ""

    signal = "BUY" if long_ok else "SELL"
    depth = int(curr["long_depth"] if long_ok else curr["short_depth"])

    if depth == 0 and REQUIRE_PULLBACK:
        # Trend continuation with no pullback. Not part of the setup.
        return None, 0.0, None, 0, ""

    if short_ok and depth > SHORT_MAX_DEPTH:
        # Mirroring the long side gives shorts a second, deeper entry the
        # source never describes. SHORT_MAX_DEPTH=1 declines it.
        return None, 0.0, None, 0, ""

    if REQUIRE_PULLBACK and REQUIRE_PIVOT_IN_PULLBACK:
        # The sequence is pullback first, fractal at its low. A pivot that
        # formed before the pullback began is a reversal signal arriving ahead
        # of the move it claims to reverse. Skipped when pullbacks are optional,
        # since that path is already an acknowledged deviation.
        pivot_depth = int(
            curr["long_pivot_depth"] if long_ok else curr["short_pivot_depth"]
        )
        if pivot_depth == 0:
            return None, 0.0, None, 0, ""

    # --- Strength score ---
    atr = curr["atr"]
    atr_ok = pd.notna(atr) and atr > 0

    # How open the EMA fan is.
    stack_score = (
        _clip01(abs(curr["ema_fast"] - curr["ema_slow"]) / (3 * atr)) if atr_ok else 0.5
    )

    # Fast-EMA momentum over the last 10 bars.
    prior_fast = df["ema_fast"].iloc[-11]
    if atr_ok and pd.notna(prior_fast):
        slope_score = _clip01(abs(curr["ema_fast"] - prior_fast) / atr)
    else:
        slope_score = 0.5

    # A shallow pullback to the fast EMA is the cleaner version of the setup;
    # a deep one to the mid means the trend is already under strain.
    depth_score = {1: 1.0, 2: 0.6}.get(depth, 0.5)

    strength = (stack_score + slope_score + depth_score) / 3

    arrow = "Green arrow (swing low)" if long_ok else "Red arrow (swing high)"
    trend = "bull" if long_ok else "bear"
    if depth == 1:
        pullback = f"pullback to the EMA{EMA_FAST}"
    elif depth == 2:
        pullback = f"pullback to the EMA{EMA_MID}"
    else:
        pullback = "no pullback"
    reason = f"{arrow} + {STRAT2_NAME} {trend} stack, {pullback}"

    if depth == 0:
        return signal, min(strength, WEAK_STRENGTH_CAP), "WEAK", depth, reason
    return signal, strength, "STRONG", depth, reason


def build_sl_tp(signal, price, depth, ema_mid, ema_slow, atr):
    """
    Stop goes one EMA beyond the deepest one price breached, offset by a
    fraction of ATR so it is not sitting exactly on a line price wicks.
    Target is a fixed RR multiple of that risk.

    Returns (sl, tp, rr, risk), or None when the stop would land on the wrong
    side of entry — which happens on fast moves and must not become an alert.
    """
    ref = ema_mid if depth == 1 else ema_slow
    buf = SL_BUFFER_ATR * atr

    if signal == "BUY":
        sl = ref - buf
        risk = price - sl
    else:
        sl = ref + buf
        risk = sl - price

    if not (risk > 0):
        return None
    if MAX_RISK_ATR > 0 and pd.notna(atr) and atr > 0 and risk > MAX_RISK_ATR * atr:
        return None

    tp = price + RR * risk if signal == "BUY" else price - RR * risk
    return sl, tp, RR, risk


def send_discord_alert(signal, price, atr, strength, tier, depth, reason,
                       ema_fast, ema_mid, ema_slow, sl, tp, rr):
    emoji = "\U0001F7E2" if signal == "BUY" else "\U0001F534"
    stars = "⭐" * max(1, round(strength * 5))
    order = ">" if signal == "BUY" else "<"
    stop_ref = EMA_MID if depth == 1 else EMA_SLOW
    content = (
        f"{emoji} **{tier} {signal}** {TICKER} @ {price:,.2f}\n"
        f"{reason}\n"
        f"Stack: EMA{EMA_FAST} {ema_fast:,.2f} {order} EMA{EMA_MID} {ema_mid:,.2f} "
        f"{order} EMA{EMA_SLOW} {ema_slow:,.2f}\n"
        f"Pullback depth: {depth} (stop referenced to EMA{stop_ref})\n"
        f"Signal strength: {strength * 100:.0f}% {stars}\n"
        f"SL: {sl:,.2f}  |  TP: {tp:,.2f}  |  R:R = 1:{rr:.2f}\n"
        f"_Not financial advice — informational only, act at your own discretion._"
    )
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def send_startup_message():
    content = (
        f"✅ **Signal bot started** — watching {TICKER} on {INTERVAL} candles\n"
        f"Trend: EMA {EMA_FAST}/{EMA_MID}/{EMA_SLOW} stacked\n"
        f"Trigger: Williams Fractals (n={FRACTAL_N}) after pullback to the "
        f"{EMA_FAST} or {EMA_MID}\n"
        f"Veto: any close beyond the {EMA_SLOW} kills the setup\n"
        f"Stop: beyond the {EMA_MID}/{EMA_SLOW} by {SL_BUFFER_ATR}×ATR | "
        f"Target: {RR}R\n"
        f"Polling every {POLL_SECONDS}s | closed candles only"
    )
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[{TICKER}] failed to send startup message: {exc}")


def bars_since(df, bar_time):
    """How many bars in this frame closed after bar_time. None if unknown."""
    if bar_time is None:
        return None
    try:
        ts = pd.Timestamp(bar_time)
    except (ValueError, TypeError):
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    if (ts.tz is None) != (df.index.tz is None):
        # Mismatched awareness would raise on comparison; treat as unknown so
        # the cooldown fails open rather than swallowing a real signal.
        return None
    return int((df.index > ts).sum())


def run_once():
    df = yf.download(TICKER, period=LOOKBACK, interval=INTERVAL, progress=False)
    if df.empty:
        print(f"[{TICKER}] no data returned this cycle, skipping")
        return

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = drop_unclosed_bar(df)

    if len(df) < MIN_BARS:
        print(
            f"[{TICKER}] only {len(df)} closed bars, need {MIN_BARS} to warm up "
            f"EMA{EMA_SLOW} - widen SIGNAL_LOOKBACK (currently {LOOKBACK}) "
            f"or use a coarser SIGNAL_INTERVAL"
        )
        return

    df = compute_indicators(df)
    signal, strength, tier, depth, reason = detect_signal(df)

    last = load_last_signal()
    last_signal = last["signal"] if last else None
    last_tier = last["tier"] if last else None

    if signal is None:
        print(f"[{TICKER}] no setup on the last closed bar (last sent: {last_tier} {last_signal})")
        return

    is_new_direction = signal != last_signal
    is_upgrade = signal == last_signal and last_tier == "WEAK" and tier == "STRONG"
    if not (is_new_direction or is_upgrade):
        print(f"[{TICKER}] {tier} {signal} already sent, nothing new")
        return

    # Cooldown is counted in bars off the stored bar time, not wall clock --
    # wall clock would misbehave across the settlement break.
    if not is_new_direction:
        elapsed = bars_since(df, last["bar_time"]) if last else None
        if elapsed is not None and elapsed < COOLDOWN_BARS:
            print(
                f"[{TICKER}] {tier} {signal} suppressed, only {elapsed} bars "
                f"since the last one (cooldown {COOLDOWN_BARS})"
            )
            return

    latest = df.iloc[-1]
    levels = build_sl_tp(
        signal, latest["Close"], depth, latest["ema_mid"], latest["ema_slow"], latest["atr"]
    )
    if levels is None:
        print(
            f"[{TICKER}] {tier} {signal} skipped: stop would sit on the wrong side "
            f"of entry (price {latest['Close']:.2f}, depth {depth})"
        )
        return

    sl, tp, rr, risk = levels
    send_discord_alert(
        signal, latest["Close"], latest["atr"], strength, tier, depth, reason,
        latest["ema_fast"], latest["ema_mid"], latest["ema_slow"], sl, tp, rr,
    )
    save_last_signal(signal, tier, depth, df.index[-1])
    print(
        f"[{TICKER}] sent {tier} {signal} alert @ {latest['Close']:.2f} "
        f"(depth {depth}, strength {strength:.2f}, risk {risk:.2f})"
    )


if __name__ == "__main__":
    print(
        f"Watching {TICKER} on {INTERVAL} candles | "
        f"Trend: EMA {EMA_FAST}/{EMA_MID}/{EMA_SLOW} stacked | "
        f"Trigger: Williams Fractals (n={FRACTAL_N}) after a pullback | "
        f"Veto: any close beyond the {EMA_SLOW} | "
        f"Stop beyond the {EMA_MID}/{EMA_SLOW}, target {RR}R | "
        f"closed candles only | polling every {POLL_SECONDS}s"
    )
    send_startup_message()
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[{TICKER}] error this cycle: {exc}")
        time.sleep(POLL_SECONDS)
