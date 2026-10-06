# Learnings and constraints

`review.py` sends this file to the reviewer model with every weekly review.
A person edits it; the reviewer never writes to it. Keep each entry short,
dated, and with its sample size, so that a finding with n=40 is never read
as one with n=400.

## Hard constraints (never propose changing)

- **Fractal confirmation lag.** A pivot at bar p is only knowable at p + n.
  The arrows are shifted forward by `SIGNAL_FRACTAL_N` bars. Removing the
  shift backtests beautifully and loses money live.
- **Closed candles only.** The in-progress candle is dropped, both at fetch
  and by clock (`SIGNAL_DROP_UNCLOSED_BAR`).
- **Session gaps.** Pivots whose window spans the daily break or a weekend
  are discarded, and pullback episodes reset across one
  (`SIGNAL_SESSION_GAP_MULT`).
- **Arrow convention.** Green = swing LOW = long trigger; red = swing HIGH =
  short trigger. Swapping them inverts every signal.
- **Alert timing.** Nothing a review suggests may change when an alert is
  sent relative to its bar. Only which setups qualify, and their levels.

## How to read the numbers

- At `SIGNAL_RR=1.5` breakeven is a 40% win rate (mean R = 0), before costs.
  Spread and slippage are not modelled; real breakeven is higher.
- Changing `SIGNAL_RR` moves breakeven. It is not a fix for a low win rate.
- With n closed trades, the standard error of mean R is about 1.22/√n at a
  40% win rate: ±0.22R at n=30, ±0.12R at n=100, ±0.06R at n=400.
- Every bucket tested is another chance of a false alarm. At 95% across
  40 buckets, about 2 will look significant by luck alone.
- Live alerts and backtest trades are different populations. The backtest
  holds one position at a time and counts its cooldown per direction; the
  live bot does neither.

## Findings

- **2026-09 (README):** GC=F 15m via yfinance, 73 trades, 2026-07-06 to
  2026-09-03. Win rate 37.0%, −0.08R per trade. Depth 1: n=47, −0.15R.
  Depth 2: n=26, +0.06R. Too small to act on.
- **2026-09 (README):** `SIGNAL_SHORT_MAX_DEPTH=1` cut 12 trades and left
  short expectancy unchanged at −0.17R. No evidence for changing the default.
- **2026-09 (README):** the pivot-in-pullback and stack-stability gates
  removed 5 of 140 qualifying bars; expectancy −0.10R → −0.08R. Kept because
  they match the source material, not because they fix anything.
- **2026-10 (legacy trades.csv, config unknown):** XAU_USD 1m, 959 trades,
  2026-08-02 to 2026-09-20. 400 targets, 559 stops: 41.7% win rate,
  about +0.04R per trade before costs. Not comparable with live alerts.

## Rejected ideas

- Raising `SIGNAL_RR` to rescue the win rate. See "How to read the numbers".
