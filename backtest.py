#!/usr/bin/env python3
"""
Backtest the live signal rules against historical bars.

The strategy this bot implements was demonstrated on a different market and a
different timeframe, so nothing here is validated until this script has run.
At RR 1.5 the breakeven win rate is 40% before costs; that is the number the
output should be read against.

The rules are *imported*, never reimplemented — compute_indicators,
detect_signal and build_sl_tp are the same functions the live bot calls. A
backtest that disagrees with the bot is worse than no backtest.

Indicators are computed once over the whole frame and then walked bar by bar.
That is safe because every column is causal: EMAs and ATR only look back, and
the fractal arrows are shifted forward by FRACTAL_N precisely so a pivot is
not readable until the bar that first makes it knowable. detect_signal is
handed a slice ending at the bar being evaluated, exactly as it is live.

Usage:
    python backtest.py                      # SIGNAL_TICKER from .env, 60 days
    python backtest.py --ticker GC=F --days 60
    python backtest.py --baseline           # Phase 1 gates off, for comparison

Entries fill at the close of the confirmation bar. One position at a time.
When a single bar's range covers both the stop and the target, it counts as a
loss — a 15m bar cannot say which came first, and the optimistic reading is
how backtests flatter themselves.
"""

import argparse
import os
import sys

import pandas as pd


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--ticker", default=None, help="Yahoo symbol (default: SIGNAL_TICKER)")
    p.add_argument("--interval", default=None, help="bar size (default: SIGNAL_INTERVAL)")
    p.add_argument("--days", type=int, default=60, help="days of history to pull")
    p.add_argument(
        "--baseline", action="store_true",
        help="run with the pivot-in-pullback and stack-stability gates off, "
             "so their effect on the numbers is measured rather than assumed",
    )
    p.add_argument("--csv", default=None, help="write the trade list to this path")
    return p.parse_args(argv)


ARGS = parse_args()

# signal_bot reads both of these at import time. The webhook is never used
# here: nothing in this script sends anything.
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://example.invalid/backtest-only")
if ARGS.ticker:
    os.environ["SIGNAL_TICKER"] = ARGS.ticker
if ARGS.interval:
    os.environ["SIGNAL_INTERVAL"] = ARGS.interval

import signal_bot  # noqa: E402  (must follow the env setup above)


def fetch_history(ticker, interval, days):
    """
    Yahoo caps a single intraday request at 60 days, so pull in 60-day pages
    and concatenate. It also refuses intraday bars older than its own
    retention window, so asking for more than it keeps returns less than
    requested rather than failing — the printed bar count is the truth.
    """
    end = pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=1)
    frames = []
    remaining = days
    while remaining > 0:
        span = min(60, remaining)
        start = end - pd.Timedelta(days=span)
        page = signal_bot.yf.download(
            ticker, start=start.date(), end=end.date(),
            interval=interval, progress=False, auto_adjust=False,
        )
        if page is None or page.empty:
            break
        if isinstance(page.columns, pd.MultiIndex):
            page.columns = page.columns.get_level_values(0)
        frames.append(page)
        end = start
        remaining -= span

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep="first")]


def resolve(highs, lows, entry_i, signal, sl, tp):
    """
    Walk forward from the bar after entry until the stop or the target is
    touched. Returns (outcome, exit_index) with outcome in
    {"win", "loss", "open"}.
    """
    for j in range(entry_i + 1, len(highs)):
        if signal == "BUY":
            hit_sl = lows[j] <= sl
            hit_tp = highs[j] >= tp
        else:
            hit_sl = highs[j] >= sl
            hit_tp = lows[j] <= tp
        if hit_sl:
            # Checked first on purpose: when one bar covers both levels the
            # order is unknowable, so the pessimistic reading wins.
            return "loss", j
        if hit_tp:
            return "win", j
    return "open", None


def run(df):
    """Walk the frame, one position at a time, and return the trade list."""
    trades = []
    open_until = -1          # bar index the current position closes on
    last_entry = {"BUY": None, "SELL": None}
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)

    for i in range(signal_bot.MIN_BARS - 1, len(df)):
        if i <= open_until:
            continue

        window = df.iloc[: i + 1]
        signal, strength, tier, depth, _ = signal_bot.detect_signal(window)
        if signal is None:
            continue

        prev = last_entry[signal]
        if prev is not None and i - prev < signal_bot.COOLDOWN_BARS:
            continue

        bar = df.iloc[i]
        levels = signal_bot.build_sl_tp(
            signal, bar["Close"], depth, bar["ema_mid"], bar["ema_slow"], bar["atr"]
        )
        if levels is None:
            continue

        sl, tp, rr, risk = levels
        outcome, exit_i = resolve(highs, lows, i, signal, sl, tp)
        trades.append(
            {
                "entry_time": df.index[i],
                "exit_time": None if exit_i is None else df.index[exit_i],
                "signal": signal,
                "tier": tier,
                "depth": depth,
                "strength": round(float(strength), 3),
                "entry": round(float(bar["Close"]), 2),
                "sl": round(float(sl), 2),
                "tp": round(float(tp), 2),
                "risk": round(float(risk), 2),
                "outcome": outcome,
                "r": rr if outcome == "win" else (-1.0 if outcome == "loss" else 0.0),
                "bars_held": None if exit_i is None else exit_i - i,
            }
        )
        last_entry[signal] = i
        open_until = exit_i if exit_i is not None else len(df)

    return trades


def summarise(label, rows):
    """One line of stats for a set of trades. Open trades score 0R."""
    closed = [t for t in rows if t["outcome"] != "open"]
    wins = sum(1 for t in closed if t["outcome"] == "win")
    win_rate = (wins / len(closed) * 100) if closed else 0.0
    expectancy = (sum(t["r"] for t in closed) / len(closed)) if closed else 0.0
    return (
        f"  {label:<22} {len(rows):>5}  {win_rate:>6.1f}%  {expectancy:>+7.2f}R  "
        f"{sum(t['r'] for t in closed):>+8.1f}R"
    )


def max_consecutive_losses(rows):
    worst = run_len = 0
    for t in rows:
        if t["outcome"] == "loss":
            run_len += 1
            worst = max(worst, run_len)
        elif t["outcome"] == "win":
            run_len = 0
    return worst


def report(trades, df, args):
    print()
    print("=" * 74)
    print(f"  {signal_bot.TICKER}  {signal_bot.INTERVAL}  |  {len(df)} closed bars")
    if len(df):
        print(f"  {df.index[0]}  ->  {df.index[-1]}")
    print(
        f"  gates: pivot_in_pullback={signal_bot.REQUIRE_PIVOT_IN_PULLBACK} "
        f"min_stack_bars={signal_bot.MIN_STACK_BARS} "
        f"short_max_depth={signal_bot.SHORT_MAX_DEPTH} "
        f"rr={signal_bot.RR}"
    )
    print("=" * 74)

    if not trades:
        print("\n  No trades. Either the filters are too tight or the window is "
              "too short.\n")
        return

    closed = [t for t in trades if t["outcome"] != "open"]
    still_open = len(trades) - len(closed)

    print(f"\n  {'':<22} {'n':>5}  {'win%':>7}  {'exp':>8}  {'total':>9}")
    print(summarise("ALL", trades))
    print()
    for direction in ("BUY", "SELL"):
        rows = [t for t in trades if t["signal"] == direction]
        if rows:
            print(summarise(direction, rows))
    for depth in (0, 1, 2):
        rows = [t for t in trades if t["depth"] == depth]
        if rows:
            print(summarise(f"depth {depth}", rows))
    for tier in ("STRONG", "WEAK"):
        rows = [t for t in trades if t["tier"] == tier]
        if rows:
            print(summarise(tier, rows))

    breakeven = 100 / (1 + signal_bot.RR)
    actual = (sum(1 for t in closed if t["outcome"] == "win") / len(closed) * 100
              if closed else 0.0)
    held = [t["bars_held"] for t in closed if t["bars_held"] is not None]

    print()
    print(f"  breakeven win rate at {signal_bot.RR}R : {breakeven:.1f}%  "
          f"(actual {actual:.1f}%)")
    print(f"  max consecutive losses      : {max_consecutive_losses(trades)}")
    mean_held = f"{sum(held) / len(held):.1f}" if held else "n/a"
    print(f"  mean bars held              : {mean_held}")
    print(f"  still open at the end       : {still_open}")
    print()
    print("  Costs are not modelled. Spread and slippage come straight off the")
    print("  expectancy above, and at 1.5R they are not a rounding error.")
    print()

    if args.csv:
        pd.DataFrame(trades).to_csv(args.csv, index=False)
        print(f"  trades written to {args.csv}\n")


def main():
    args = ARGS

    if args.baseline:
        # Reproduces the behaviour before the pivot-in-pullback and stack
        # stability gates existed, so their cost in trade count and their
        # effect on expectancy are measured rather than argued about.
        signal_bot.REQUIRE_PIVOT_IN_PULLBACK = False
        signal_bot.MIN_STACK_BARS = 1

    if not signal_bot.TICKER:
        print("SIGNAL_TICKER is not set. Pass --ticker or set it in .env.",
              file=sys.stderr)
        return 2

    df = fetch_history(signal_bot.TICKER, signal_bot.INTERVAL, args.days)
    if df.empty:
        print(f"No data returned for {signal_bot.TICKER}.", file=sys.stderr)
        return 1

    df = signal_bot.drop_unclosed_bar(df)
    if len(df) < signal_bot.MIN_BARS:
        print(f"Only {len(df)} closed bars, need {signal_bot.MIN_BARS}.",
              file=sys.stderr)
        return 1

    df = signal_bot.compute_indicators(df)
    report(run(df), df, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
