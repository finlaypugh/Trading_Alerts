# Trading_Alerts

A notification bot. It watches one Yahoo Finance symbol, looks for a single
setup — a Williams Fractal reversal inside a pullback within a stacked
triple-EMA trend — and posts a Discord message when it finds one.
**It never places a trade.** There is no broker integration, no order code and
no key with trading permissions anywhere in this repo. Every alert is a
suggestion for you to act on by hand, or ignore.

## Setup

```bash
cp .env.example .env      # then fill in DISCORD_WEBHOOK_URL and SIGNAL_TICKER
./run.sh                  # macOS / Linux
run.bat                   # Windows
```

Both launchers create a virtualenv, install `requirements.txt`, load `.env`
and start the bot. Neither has a default ticker: an unset `SIGNAL_TICKER` is
an error, because a silent fallback would run a gold-tuned strategy against
whatever the fallback happened to be.

To keep it running across reboots, use PM2:

```bash
pm2 start ./run.sh --name trading-alerts
pm2 save && pm2 startup
```

Tests:

```bash
pip install pytest && pytest -v
```

## The strategy

Stated the way the source material states it:

1. Put the 20, 50 and 100 EMAs on the chart, plus Williams Fractals with a
   period of 2.
2. **Long only** when the MAs are stacked 20 above 50 above 100. **Short only**
   when they are stacked the other way. When they are crossing or tangled,
   that is the third state: **no trade**.
3. Wait for price to pull back to the 20 or the 50.
4. Enter when a fractal prints at the turn of that pullback — a green arrow
   (swing low) for a long, a red arrow (swing high) for a short.
5. Stop one MA beyond the deepest one the pullback reached: pulled back to the
   20, stop below the 50; pulled back to the 50, stop below the 100.
6. Target 1.5× the risk.
7. If price closes beyond the 100, the setup is dead — no entry.

The green/red mapping looks inverted at first glance and is not. Green marks a
swing **low** and is the long trigger; the source notes the indicator's default
colours read backwards. `test_green_arrow_maps_to_the_swing_low` pins it.

## Deviations from the source

Read these before trusting an alert.

- **Timeframe and instrument.** The source demonstrates this on 1-minute
  charts. This bot ships configured for `GC=F` (COMEX gold futures) on 15m
  bars. That is a different market structure, and the parameters were not
  re-tuned for it. See [Validation](#validation) for what that costs.
- **The two-bar confirmation lag.** A fractal at bar *p* is only knowable at
  bar *p + 2*, because the two bars to its right have to close first.
  TradingView draws the arrow back at the pivot, so every chart example looks
  like an entry two bars earlier — and two bars better — than a live bot can
  manage. Every arrow here is shifted forward by `FRACTAL_N`. This is not a
  tuning parameter; removing it backtests beautifully and loses money live.
- **Pullback depth is measured on closes, not wicks.** A bar that wicks below
  the 20 but closes above it is not a pullback. The source is ambiguous here;
  close-based is the stricter reading.
- **The pivot must sit inside the pullback.** The source's sequence is pullback
  first, fractal at its low. A naive implementation only checks that a pullback
  exists on the *confirmation* bar, which lets a fractal that formed before the
  pullback started fire a reversal signal ahead of the move it claims to
  reverse. `SIGNAL_REQUIRE_PIVOT_IN_PULLBACK` (on by default) rejects those.
- **"Disorderly" is given a number.** The source says not to trade while the
  MAs are crossing but never quantifies it. `SIGNAL_MIN_STACK_BARS=3` requires
  the stack to have held its order for three closed bars;
  `SIGNAL_MIN_STACK_SEP_ATR` (off by default) is the separate knob for a fan
  that is ordered but too tight.
- **Session gaps.** Gold has a daily settlement break and weekends. Pivots
  whose window straddles a break are discarded and pullback episodes reset
  across one, otherwise the first bar back after the weekend reliably fakes a
  pivot. The source, on a 1m crypto-style chart, never has to deal with this.
- **Shorts are mirrored.** The source describes exactly one short entry —
  pullback above the 20, stop above the 50. This bot mirrors the long side in
  full, so it also takes the deeper pullback-above-the-50 short.
  `SIGNAL_SHORT_MAX_DEPTH=1` restricts it to the literal reading.
- **The WEAK tier.** With `SIGNAL_REQUIRE_PULLBACK=false` the bot will alert on
  a fractal in a stacked trend with no pullback at all, capped at a lower
  strength score. The source does not endorse no-pullback entries; this exists
  to compare behaviour, not because it is part of the setup.

## Validation

`backtest.py` replays the live rules over historical bars. It imports
`compute_indicators`, `detect_signal` and `build_sl_tp` rather than
reimplementing them — a backtest that disagrees with the bot is worse than no
backtest.

```bash
python backtest.py --ticker 'GC=F' --days 60
python backtest.py --ticker 'GC=F' --days 60 --baseline   # gates off
```

Entries fill at the close of the confirmation bar, one position at a time,
honouring `SIGNAL_COOLDOWN_BARS`. When one bar's range covers both the stop
and the target it is scored as a loss — a 15m bar cannot say which came first.

**Result, `GC=F` 15m, 3,993 bars (2026-07-06 to 2026-09-03):**

| | trades | win rate | expectancy |
|---|---|---|---|
| all | 73 | 37.0% | −0.08R |
| longs | 40 | 40.0% | ±0.00R |
| shorts | 33 | 33.3% | −0.17R |
| depth 1 (pullback to the 20) | 47 | 34.0% | −0.15R |
| depth 2 (pullback to the 50) | 26 | 42.3% | +0.06R |

At 1.5R the breakeven win rate is 40%. This window came in at 37%, before
spread and slippage, which are not modelled and which come straight off that
number. **On this instrument and timeframe the strategy did not clear its own
breakeven over the only window Yahoo will serve intraday bars for.** Treat the
alerts as prompts to go and look at a chart, not as an edge.

Two secondary findings from the same window, both worth knowing and neither
strong enough to act on alone:

- The pivot-in-pullback and stack-stability gates removed 5 of 140 qualifying
  bars and moved expectancy from −0.10R to −0.08R. They are defensible on the
  grounds that they match what the source actually says; they are not a fix.
- `SIGNAL_SHORT_MAX_DEPTH=1` cut 12 trades and left short expectancy unchanged
  at −0.17R, so there is no evidence for making it the default. It stays at 2.

Sixty days is a small sample and one regime. Do not over-read any of it.

## Configuration

All configuration is environment variables, read once at import. `.env.example`
is the annotated copy; this table is the complete list.

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | **Required.** Where alerts are posted. |
| `SIGNAL_TICKER` | — | **Required** by the launchers. Yahoo symbol, e.g. `GC=F`. |
| `SIGNAL_INTERVAL` | `15m` | Bar size: `1m`, `5m`, `15m`, `1h`, `1d`. |
| `SIGNAL_LOOKBACK` | `10d` | History pulled each poll. Must clear the EMA warm-up. |
| `SIGNAL_EMA_FAST` | `20` | Fast EMA. |
| `SIGNAL_EMA_MID` | `50` | Mid EMA. Depth-1 stop reference. |
| `SIGNAL_EMA_SLOW` | `100` | Slow EMA. Depth-2 stop reference, and the veto line. |
| `SIGNAL_FRACTAL_N` | `2` | Bars either side of a pivot. Also the confirmation lag. |
| `SIGNAL_FRACTAL_MAX_PLATEAU` | `4` | Equal bars tolerated on a pivot's left side. |
| `SIGNAL_PULLBACK_EXPIRY_BARS` | `3` | Bars back above the fast EMA that close an episode. |
| `SIGNAL_REQUIRE_PULLBACK` | `true` | `false` allows no-pullback WEAK alerts. |
| `SIGNAL_REQUIRE_PIVOT_IN_PULLBACK` | `true` | Reject a fractal that formed before the pullback. |
| `SIGNAL_MIN_STACK_BARS` | `3` | Closed bars the stack must hold its order for. |
| `SIGNAL_SHORT_MAX_DEPTH` | `2` | `1` restricts shorts to the pullback-above-20 entry. |
| `SIGNAL_RR` | `1.5` | Target as a multiple of risk. |
| `SIGNAL_SL_BUFFER_ATR` | `0.25` | Stop offset beyond the reference EMA, in ATRs. |
| `SIGNAL_MIN_STACK_SEP_ATR` | `0.0` | Minimum fast-to-slow separation, in ATRs. `0` = off. |
| `SIGNAL_MAX_RISK_ATR` | `0.0` | Reject setups whose stop is wider than this. `0` = off. |
| `SIGNAL_ATR_LEN` | `14` | ATR period (Wilder smoothing). |
| `SIGNAL_DROP_UNCLOSED_BAR` | `true` | Ignore the in-progress candle yfinance returns. |
| `SIGNAL_SESSION_GAP_MULT` | `2.0` | Intervals of silence that count as a session break. |
| `SIGNAL_COOLDOWN_BARS` | `4` | Minimum bars between same-direction alerts. |
| `SIGNAL_WEAK_STRENGTH_CAP` | `0.5` | Ceiling on a WEAK alert's strength score. |
| `SIGNAL_POLL_SECONDS` | `300` | Seconds between polls. |

Alerts are throttled by state in `.state_<ticker>.json`: a repeat of the same
direction and tier is suppressed, a WEAK→STRONG upgrade is not, and the
cooldown is counted in bars rather than wall clock so it behaves across the
settlement break. State is written only after Discord confirms delivery, so a
dropped alert is retried rather than recorded as sent.

## Not financial advice

Informational only. You are responsible for anything you do with these alerts.
