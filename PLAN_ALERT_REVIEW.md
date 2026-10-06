# Plan: Alert outcome log (A) + weekly LLM reviewer (B)

**Status:** plan only, no code changed. **Complexity:** A = Medium, B = Medium. **Order:** A ships and collects data before any of B except its stats-only mode.

---

## 0. Corrections to the brief (checked against the code at `7d1b07c`)

| Brief says | Code actually does | Consequence for the plan |
|---|---|---|
| GC=F, 15m | OANDA `XAU_USD` spot mid candles (`signal_bot.fetch_candles`). Your `.env` and `.env.example` both set `SIGNAL_INTERVAL=1m`. Only the tests force 15m (`conftest.py:15`). | Store `ticker` and `interval` on every row, and never assume 15m. |
| yfinance flakiness | There is no yfinance anywhere. Failure modes are OANDA `HTTPError` (401/429/5xx), timeouts, an empty frame, and missing no-tick minute candles. | The resolver handles OANDA errors (§A4). |
| `append_alert` is append-only | It rewrites the whole `.alerts_<T>.jsonl` atomically and keeps the **newest 500** (`ALERTS_KEEP`). At the backtest's rate (959 trades in 49 days, about 20 a day) that is roughly 25 days of history. | The JSONL can't be the permanent log. It becomes the staging file for the DB. |
| `append_alert` needs signal, tier, depth, sl, tp, rr, entry, bar_time | It already records `sent_utc, bar_time, signal, tier, depth, strength, price, sl, tp, rr, risk, reason` (`signal_bot.py:1014`). | Only `atr, ema_*, stack_bars, bars_since_last_alert, alert_id, ticker, interval, config_hash` are new. Keep `price`; don't rename it to `entry` in the JSONL, because the dashboard reads `price`. |
| `ema_touched`, `hour_utc` | `depth` is the EMA touched (1 = closed beyond EMA20, 2 = beyond EMA50). The hour comes from `bar_time`. | Derive both at query time and don't store them. |
| `outcomes.resolve()` gives outcome, exit_time, r | It returns `(outcome, exit_index)` only. `r` is computed separately in `backtest.py:137` (`rr`) and `dashboard/status.py:281` (`abs(tp-price)/abs(price-sl)`). | Add one `outcomes.r_multiple()` and use it in all three places. |
| `trades.csv` schema | There is no fixed file. `backtest.py --csv PATH` writes `entry_time, exit_time, signal, tier, depth, strength, entry, sl, tp, risk, outcome, r, bars_held`. Your local `trades.csv` (959 rows, 2026-08-02 → 09-20) uses an **older schema**: `outcome` is `TP`/`SL`, it has `reason`/`rr`, and it has no `bars_held`. It is also **untracked and not gitignored**. | The backfill handles both schemas (§A5), and `.gitignore` gains `trades*.csv`. |
| `.state_*.json` | It holds a single last-signal slot `{signal, tier, depth, bar_time}` shared by both directions. | Read-only for this plan. |
| Live Discord webhook committed in `.env` in the public repo | `.env` is **not tracked at HEAD** and has been gitignored since `8a555b2` (TA-4). A webhook **was** committed in `.env` in `ac11f0b`/`94101ec` (2026-08-13) and deleted in `859a213` (2026-08-16), so it is still in public history. A hash comparison shows it is a **different webhook ID from the one in your current `.env`**. | Delete the *old* webhook in Discord if it still exists. Rotating the current one is optional hygiene. See §S. |
| Dashboard shows outcomes | Outcomes are recomputed on every request from `.bars_<T>.json` (1500 bars, about 1 day of 1m), so older alerts show `unknown` (`status.py:272`). | This is the gap Feature A closes. |
| Goal: stop repeated duplicate alerts | Same-bar repeats are already suppressed (`elapsed == 0`), and same-direction alerts have a 4-bar cooldown. Three duplicate paths remain: **(a)** a Discord POST that times out *after* delivery raises before `append_alert`/`save_last_signal`, so the next poll re-sends the same bar; **(b)** live has no one-position-at-a-time rule, but the backtest does (`open_until`); **(c)** the live cooldown keys off a single slot, so BUY→SELL→BUY inside 4 bars passes live but is blocked in the backtest (per-direction `last_entry`). | All three are alert-timing changes and **out of scope**. Feature A *measures* them: duplicate `alert_id` count, an `overlaps_open` bucket, and a `prev_same_dir_outcome` bucket. Fixes come later, backed by that evidence. |

---

## Patterns to mirror

| Category | Source | Pattern |
|---|---|---|
| Never-raise side outputs | `signal_bot.py:351` `write_status`, `:413` `append_alert` | `try/except Exception → print(f"[{TICKER}] failed to …: {type(exc).__name__}: {exc}")` |
| Atomic file write | `signal_bot.py:332` `_atomic_write` | temp file + `os.replace` |
| Import-safe shared module | `outcomes.py` | no project imports, so the dashboard and backtest can both use it |
| Dashboard reads | `dashboard/status.py:112` `_read_json` | `(data, error)`; a missing file is a message, not an exception; allowlisted fields; `_clean`/`redact` |
| Secrets | `dashboard/status.py:73` `SECRET_ENV`, `app.py:149` response backstop | add `ANTHROPIC_API_KEY` |
| Config | `signal_bot.py:73-126` | `os.environ.get("KEY", default)` at module level, documented in `.env.example` |
| Tests | `test_signal_bot.py:839` `run_once_env`, `test_backtest.py:43` `FakeOanda` | `class TestX`, `monkeypatch.setattr(module, "NAME", …)`, `tmp_path`, fakes rather than network |

---

## Architecture

```
signal_bot._poll ─ send_discord_alert ─▶ append_alert(+new fields) ─▶ .alerts_<T>.jsonl   (bot = only writer; same mechanism as today)
                                                                        │
resolve_alerts.py  (timer, every 15 min) ── ingest (INSERT OR IGNORE) ─▶ .alerts_<T>.db     (resolver = only writer)
        └─ fetch_candles (OANDA) ─ outcomes.resolve ─ UPDATE pending/open rows
                                                                        │ read-only
dashboard  /api/alerts (outcome from DB) · /api/stats (bucket table) ◀──┤
review.py  (timer, weekly) ── stats in code ─ Claude ─ guardrails ─▶ reports/YYYY-MM-DD.md (+ optional Discord)
```

The bot's alert path gains **one dict of extra kwargs**, built after the Discord send. Nothing new can raise into `_poll`, and nothing here touches `detect_signal`, the cooldown, `build_sl_tp`, or the send order.

---

## Feature A: alert and outcome log

### A1. Storage decision: **SQLite** (stdlib `sqlite3`, WAL mode)

| Concern | CSV / JSONL | SQLite (WAL) |
|---|---|---|
| Bot, resolver and dashboard at once | Whole-file rewrite means the resolver's read-modify-replace can silently drop an alert the bot appended in between | Transactions; readers don't block the writer; `busy_timeout` |
| Updating outcomes in place | Rewrite everything | `UPDATE … WHERE outcome IN ('pending','open')` |
| Power cut on the Pi's SD card | Atomic replace is safe, but any in-place append can tear | Journalled atomic commit; `synchronous=FULL` (volume is tiny) |
| Windows | `os.replace` onto a file another process holds open can fail with `PermissionError` | Native locking |
| Dependencies | none | none (stdlib; SQLite 3.50 locally, ≥3.7 on any Pi OS) |
| Bucket queries | pandas | `GROUP BY`, or the same Python over rows |

The bot keeps writing JSONL and never opens the DB. That keeps every new failure mode out of the alert path and leaves exactly one DB writer. The DB is **`.alerts_<T>.db`**, which the existing `.gitignore` rule `.alerts_*` already covers, including `-wal` and `-shm`. Dashboard and reviewer connect with `file:…?mode=ro` (same user as the resolver, so WAL `-shm` access works).

### A2. New module `alert_log.py` (import-safe, stdlib only, like `outcomes.py`)

```python
SCHEMA_VERSION = 1
DDL = """
CREATE TABLE IF NOT EXISTS alerts (
  source TEXT NOT NULL,              -- 'live' | 'backtest:<run_id>' | 'backtest:legacy'
  alert_id TEXT NOT NULL,            -- make_alert_id(ticker, signal, bar_time)
  ticker TEXT, interval TEXT, config_hash TEXT,
  bar_time TEXT NOT NULL,            -- ISO-8601 UTC, bar the alert fired on (entry = its close)
  sent_utc TEXT, signal TEXT NOT NULL, tier TEXT, depth INTEGER, strength REAL,
  entry REAL, sl REAL, tp REAL, rr REAL, risk REAL, atr REAL,
  ema_fast REAL, ema_mid REAL, ema_slow REAL,
  stack_bars INTEGER, bars_since_last_alert INTEGER, reason TEXT,
  outcome TEXT NOT NULL DEFAULT 'pending',  -- pending|open|win|loss|expired|unresolvable
  exit_time TEXT, r REAL, bars_held INTEGER, exit_gapped INTEGER,
  resolved_through TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  updated_utc TEXT,
  PRIMARY KEY (source, alert_id)
);
CREATE INDEX IF NOT EXISTS ix_open ON alerts(outcome) WHERE outcome IN ('pending','open');
"""
PRAGMAS = ("journal_mode=WAL", "synchronous=FULL", "busy_timeout=5000")

def make_alert_id(ticker, signal, bar_time):
    """Stable across bot, backtest and backfill: bar_time normalised to ISO UTC."""
    ts = datetime.fromisoformat(str(bar_time).replace(" ", "T"))
    ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
    return f"{ticker}|{signal}|{ts.isoformat()}"

def connect(path, readonly=False): ...      # readonly: f"file:{path}?mode=ro", uri=True
def ingest_jsonl(conn, path) -> (inserted, skipped, dupes): ...  # INSERT OR IGNORE, source='live'
def import_trades_csv(conn, path, source): ...                   # §A5
def bucket_stats(rows, dim, min_n, k_tests, alpha): ...          # §A6
```

- `PRIMARY KEY (source, alert_id)` lets live and backtest rows for the same bar coexist, and their join answers "did live fire where the backtest did?"
- `ingest_jsonl` counts JSONL lines whose `alert_id` is already present with a **different `sent_utc`**. Those are true duplicate sends (path (a) above) and the report shows the count.
- Gap guard: if the oldest JSONL line is newer than the newest live DB row, alerts were truncated away before ingest. Log `WARNING: alert log gap`.

### A3. Bot change: the only edit to `signal_bot.py`

```python
CONFIG_HASH = hashlib.sha256(json.dumps([            # strategy knobs only, no secrets
    INTERVAL, EMA_FAST, EMA_MID, EMA_SLOW, FRACTAL_N, FRACTAL_MAX_PLATEAU,
    PULLBACK_EXPIRY_BARS, REQUIRE_PULLBACK, REQUIRE_PIVOT_IN_PULLBACK, MIN_STACK_BARS,
    SHORT_MAX_DEPTH, RR, SL_BUFFER_ATR, MIN_STACK_SEP_ATR, MAX_RISK_ATR, ATR_LEN,
    SESSION_GAP_MULT, COOLDOWN_BARS]).encode()).hexdigest()[:12]

def _alert_context(df, last, signal):
    """Extra alert-log fields. Read-only over df; never raises."""
    try:
        curr, bar = df.iloc[-1], df.index[-1]
        since = None
        if last and last.get("bar_time"):
            ts = pd.Timestamp(last["bar_time"])
            since = bars_since(df, ts) if ts >= df.index[0] else None   # None = older than lookback
        return {
            "alert_id": alert_log.make_alert_id(TICKER, signal, bar),
            "ticker": TICKER, "interval": INTERVAL, "config_hash": CONFIG_HASH,
            "atr": _finite(curr["atr"]), "ema_fast": _finite(curr["ema_fast"]),
            "ema_mid": _finite(curr["ema_mid"]), "ema_slow": _finite(curr["ema_slow"]),
            "stack_bars": int(curr["bull_stack_bars" if signal == "BUY" else "bear_stack_bars"]),
            "bars_since_last_alert": since,
            "last_alert_signal": last["signal"] if last else None,
        }
    except Exception as exc:
        print(f"[{TICKER}] failed to build alert context: {type(exc).__name__}: {exc}")
        return {}

# in _poll, at the existing append_alert call (after send_discord_alert, unchanged position):
append_alert(bar_time=..., signal=..., ..., reason=reason, **_alert_context(df, last, signal))
```

- [ ] `bars_since` itself stays unchanged, because the cooldown uses it.
- [ ] `ALERTS_KEEP` stays at 500. The resolver ingests every 15 minutes, far inside the roughly 25-day window.
- [ ] Add `CONFIG_HASH` to `write_status` fields and to `dashboard/status.py` `STATUS_FIELDS`, so the UI shows which config produced the stats.

### A4. Resolver: `resolve_alerts.py` (CLI, imports `signal_bot` for `fetch_candles`/`oanda_granularity`)

```
python resolve_alerts.py                      # ingest + resolve (what the timer runs)
python resolve_alerts.py --dry-run            # print what would change, write nothing
python resolve_alerts.py backfill trades.csv --source backtest:legacy
```

```python
def resolve_pending(conn, fetch=signal_bot.fetch_candles, now=None):
    rows = conn.execute("SELECT * FROM alerts WHERE source='live' AND outcome IN ('pending','open') "
                        "ORDER BY bar_time").fetchall()
    for (ticker, interval), grp in group_by(rows, ("ticker", "interval")):
        start = min(r["bar_time"] for r in grp)
        df = fetch(ticker, signal_bot.oanda_granularity(interval), start, now)   # OUTSIDE any txn
        highs, lows, opens = (df[c].to_numpy(float) for c in ("High", "Low", "Open"))
        pos = {to_iso(t): i for i, t in enumerate(df.index)}
        updates = []
        for r in grp:
            i = pos.get(r["bar_time"])
            if i is None:                                   # entry bar not in OANDA's history
                updates.append(missing(r, max_attempts=3))  # → 'unresolvable' after 3 runs
                continue
            outcome, j = outcomes.resolve(highs, lows, i, r["signal"], r["sl"], r["tp"])
            if outcome == "open":
                expired = age(r["bar_time"], now) > MAX_HOLD
                updates.append(open_or_expired(r, df.index[-1], expired))
            else:
                updates.append(final(r, outcome, df.index[j], j - i,
                                     outcomes.r_multiple(outcome, r["entry"], r["sl"], r["tp"]),
                                     exit_gapped=gapped(opens[j], r)))
    with conn:  # BEGIN … COMMIT; the guard keeps final rows final on re-run
        conn.executemany("UPDATE alerts SET … WHERE source=? AND alert_id=? "
                         "AND outcome IN ('pending','open')", updates)
```

- [ ] **Idempotent:** an outcome is a pure function of the alert and the candles. Final rows are never matched by the `UPDATE` guard. Open rows are re-walked from `bar_time` every run, which is correct and costs a bounded fetch: `RESOLVE_MAX_HOLD_DAYS` × 1440 M1 candles is at most 2 OANDA pages.
- [ ] **OANDA failure** (`requests.RequestException`, `HTTPError`, empty frame): ingest still commits, resolution writes nothing, the error is logged through `redact`, and the exit code is 1. The next timer tick retries.
- [ ] **Data gaps:** OANDA omits minutes with no ticks, and `resolve()` walks rows, so a missing quiet minute can't hide a touch. A weekend or daily-break gap through the stop is still scored at −1R, but `exit_gapped=1` (exit bar opened beyond the level) flags that the real fill was worse.
- [ ] **Entry sanity:** if the refetched close of `bar_time` differs from the logged `entry` by more than 0.01, set `last_error='entry_mismatch'`. Resolve anyway, and surface the count in stats.
- [ ] **Timer:** `deploy/resolve-alerts.service` (oneshot) plus `.timer` with `OnCalendar=*:0/15` and `Persistent=true`. Being oneshot means it never overlaps itself. On Windows, use Task Scheduler with "do not start a new instance" and a `run_resolver.bat` that mirrors `run_dashboard.bat`.

### A5. Backfill: can `trades.csv` share the table?

**Yes, with a `source` column. It must never be pooled with live data by default.**

```python
LEGACY = {"TP": "win", "SL": "loss"}                 # your 959-row file
def import_trades_csv(conn, path, source):
    for t in csv.DictReader(open(path)):
        outcome = LEGACY.get(t["outcome"], t["outcome"])   # new schema already win/loss/open
        row = dict(source=source, alert_id=make_alert_id(TICKER, t["signal"], t["entry_time"]),
                   bar_time=iso(t["entry_time"]), entry=f(t["entry"]), outcome=outcome,
                   r=f(t["r"]), config_hash=t.get("config_hash") or None, ...)  # missing cols → NULL
        conn.execute("INSERT OR IGNORE INTO alerts …", row)
```

Why the sources are kept separate:
- The backtest is one position at a time, has a per-direction cooldown, evaluates every bar, and has no Discord failures. Live does none of those, so they are different populations.
- The legacy file has no `atr/stack_bars/bars_since`, and its config is unknown (`config_hash=NULL`).

Checklist:
- [ ] Import the legacy file once as `backtest:legacy`. It serves as the prior only.
- [ ] Better: extend `backtest.run()` to add `atr, ema_*, stack_bars, bars_since_last_alert, config_hash` to each trade dict, using the same columns the bot logs, and add `--db` to write rows as `backtest:<utc-timestamp>`. These are additive CSV columns, and there are no rule changes.
- [ ] Replace `backtest.py:137` `r` and `dashboard/status.py:280-283` with `outcomes.r_multiple`.

### A6. Bucket stats (shared by dashboard and `review.py`)

```python
def bucket_stats(rows, dim, min_n=30, k_tests=1, alpha=0.05):
    """rows: resolved win/loss only. Returns one dict per bucket of `dim`."""
    z = NormalDist().inv_cdf(1 - alpha / (2 * k_tests))     # Bonferroni across k buckets
    for key, rs in groupby_dim(rows, dim):
        n = len(rs); p = sum(r > 0 for r in rs) / n
        mean = fmean(rs); se = stdev(rs) / sqrt(n) if n > 1 else None
        yield dict(bucket=f"{dim}={key}", n=n, win_rate=p, win_rate_se=sqrt(p*(1-p)/n),
                   mean_r=mean, se_r=se, noise=n < min_n,
                   sig_negative=(not n < min_n) and se is not None and mean + z * se < 0)
```

| Dimension | Buckets (derived at query time) |
|---|---|
| `signal` | BUY / SELL |
| `depth` | 1 (EMA20) / 2 (EMA50) |
| `session` (from `hour_utc`) | Asia 22–07, London 07–12, NY 12–17, Late 17–22. 24 hourly buckets only on request. |
| `since_last` (`bars_since_last_alert`) | none/>lookback, 4–9, 10–29, 30+ |
| `stack_bars` | 3–9, 10–49, 50+ |
| `strength` | terciles |
| `prev_same_dir_outcome` | win / loss / open / none. **This is the "repeating losing alerts" question.** |
| `overlaps_open` | was an earlier alert still open at `bar_time`? (from `exit_time`). **This is the duplicate-stacking question.** |

How much data each bucket needs. At RR 1.5 and a 40% win rate, the standard deviation of R is 2.5·√(0.4·0.6) ≈ **1.22R**, so the standard error of mean R ≈ 1.22/√n:

| n | 30 | 100 | 400 |
|---|---|---|---|
| SE(mean R) | 0.22R | 0.12R | 0.06R |

Below about 100 resolved trades, a bucket can't tell −0.2R from breakeven. The UI greys out `n < STATS_MIN_N` rows and labels them "noise".

### A7. Dashboard (cheap: about 80 lines of Python and 60 of JS, so it's included)

- [ ] `status.load_alerts`: after `add_outcomes`, overwrite `outcome/r/exit_ts` from the DB by `alert_id` when the DB row is final. That fixes old alerts showing as `unknown`. Keep the bars-file path for fresh alerts and live R.
- [ ] `status.load_stats(dim, source="live", config_hash=current)` → `GET /api/stats?by=depth`. A missing or locked DB returns `{"rows": [], "message": …}` and never a 500.
- [ ] `index.html` gets a `<details>` card titled "Bucket stats" with a dimension `<select>` and a table of n / win% ± SE / mean R ± SE. It shows a footer with "k buckets shown; at 95% about k/20 look significant by chance", the breakeven line (40% at 1.5R), and greys out `noise` rows.
- [ ] Add `ANTHROPIC_API_KEY` to `SECRET_ENV`.

---

## Feature B: weekly LLM reviewer (offline, advisory)

### B1. Shape

```
review.py [--no-llm] [--dry-run] [--days 28] [--post-discord]
  1 rows   = live, resolved (win|loss), current config_hash, last REVIEW_WINDOW_DAYS, newest REVIEW_MAX_ALERTS
  2 if len(rows) < REVIEW_MIN_N → write "Insufficient data: n=…, need …" report, NO API call, exit 0
  3 stats  = alert_log.bucket_stats over all dims (k = total buckets → Bonferroni z); backtest prior table
  4 --no-llm → write the stats-only report and stop   ← shippable in Phase A
  5 call Claude with structured output (B3)
  6 guardrails (B4) → validated proposals, rejected proposals with the reason
  7 render reports/YYYY-MM-DD.md: every NUMBER is rendered by code; the LLM supplies prose plus bucket_ids
  8 optional Discord summary (≤1900 chars, redacted, same webhook)
```

- [ ] Never imported by `signal_bot.py` or `dashboard/`. A test enforces this.
- [ ] `import anthropic` happens lazily inside `call_llm`, from a new `requirements-review.txt`. The bot and dashboard install nothing new.
- [ ] `deploy/review.service` (oneshot) plus `review.timer` with `OnCalendar=Sat *-*-* 12:00:00 UTC` and `Persistent=true`. Gold is shut from Friday evening to Sunday evening, so that week's trades are resolved by then.
- [ ] `docs/LEARNINGS.md` is tracked and human-edited only. It holds the hard constraints, past findings (with n and date), and rejected ideas. The LLM never writes to it; a report can *suggest* lines to add.

### B2. Model, cost, keys

| Item | Choice |
|---|---|
| Model | `claude-opus-5-5`, set via `REVIEW_MODEL`. `claude-sonnet-5-5` costs about half and is your call. |
| Call | `client.beta.messages.create(..., max_tokens=16000, output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")`, non-streaming, one call. Server-side refusal fallback is on by default and can be dropped. |
| Input | about 20–30k tokens: system prompt and LEARNINGS (~3k), stats (~3k), ≤400 alerts as compact CSV (~60 tokens each) |
| Cost per run | input ~25k × $4/M ≈ $0.10, plus output and thinking ~5–10k × $20/M ≈ $0.10–0.20, so **about $0.20–0.30 a run, roughly $1–1.30 a month**. `--dry-run` prints `messages.count_tokens`. |
| Key | `ANTHROPIC_API_KEY` in `.env` (gitignored), read by the SDK's default resolution. It is never logged or put in reports, and it is added to `SECRET_ENV`. |
| Failure | Catch `anthropic.APIStatusError`, `APIConnectionError`, `stop_reason in ("refusal", "max_tokens")`, JSON or schema errors, and timeouts. Any of these writes a report with "Review failed: <redacted error>" plus the stats-only section, then exits 1 so the systemd unit shows red. It can't affect the bot because it is a separate process with no shared files written. |

### B3. Prompt template and output schema

```text
SYSTEM
You review alerts from a rules-based XAU_USD pullback strategy. You are advisory; a human decides.
Facts you must use:
- RR = {rr}. Breakeven win rate = 1/(1+RR) = {breakeven:.0%} (40% at 1.5R) before costs. Costs are not modelled.
- Every number you need is in STATS. Do not compute or restate statistics; refer to buckets by bucket_id.
- {k} buckets were tested. At 95% about {k}/20 look significant by chance. Only buckets with
  sig_negative=true (n ≥ {bucket_min_n}, Bonferroni-corrected) may justify a proposal.
- If nothing qualifies, verdict = "insufficient_data" or "no_action". Never guess.
- You may only propose values for keys in ALLOWED_KEYS. You may not propose code changes; put ideas that
  need code (new filters) in "hypotheses" with a backtest to run.
- Hard constraints (never propose changing): {from docs/LEARNINGS.md}
USER
CONFIG (current .env strategy keys): {json}
STATS (live, config {hash}, {start}→{end}, n={n}): {json}
BACKTEST PRIOR: {json or "none"}
ALERTS (csv): {alert_id,signal,depth,session,since_last,stack_bars,strength,outcome,r}
LEARNINGS: {docs/LEARNINGS.md}
```

```json
{"type":"object","additionalProperties":false,
 "required":["verdict","summary","loss_clusters","negative_buckets","proposals","hypotheses","caveats"],
 "properties":{
  "verdict":{"enum":["insufficient_data","no_action","proposals"]},
  "summary":{"type":"string"},
  "loss_clusters":{"type":"array","items":{"type":"object","additionalProperties":false,
     "required":["description","bucket_ids","alert_ids"],
     "properties":{"description":{"type":"string"},
       "bucket_ids":{"type":"array","items":{"type":"string"}},
       "alert_ids":{"type":"array","items":{"type":"string"}}}}},
  "negative_buckets":{"type":"array","items":{"type":"object","additionalProperties":false,
     "required":["bucket_id","interpretation"],
     "properties":{"bucket_id":{"type":"string"},"interpretation":{"type":"string"}}}},
  "proposals":{"type":"array","items":{"type":"object","additionalProperties":false,
     "required":["env_key","proposed_value","bucket_ids","rationale","backtest_command"],
     "properties":{"env_key":{"type":"string"},"proposed_value":{"type":"string"},
       "bucket_ids":{"type":"array","items":{"type":"string"}},
       "rationale":{"type":"string"},"backtest_command":{"type":"string"}}}},
  "hypotheses":{"type":"array","items":{"type":"string"}},
  "caveats":{"type":"array","items":{"type":"string"}}}}
```

### B4. Guardrails (enforced in code, not left to the prompt)

- [ ] **Sample gate:** if `n < REVIEW_MIN_N`, skip the API call entirely.
- [ ] **Proposal check:** each proposal needs **every** `bucket_id` to exist and have `sig_negative=true`. It also needs `env_key ∈ ALLOWED_KEYS`, which are the strategy keys parsed from `.env.example` (never `DISCORD_*`, `OANDA_*`, `DASHBOARD_*`, `ANTHROPIC_*`). Finally, `proposed_value` must parse with the same type as the current value and change it. Anything that fails goes under "Rejected by guardrail" with the reason.
- [ ] If the verdict is `proposals` but no proposal survives, the report verdict becomes `no_action`.
- [ ] The diff is built by code from the surviving proposals:
  ```diff
  - SIGNAL_SHORT_MAX_DEPTH=2
  + SIGNAL_SHORT_MAX_DEPTH=1   # bucket depth=2∧signal=SELL: n=142, mean −0.31R ± 0.10 (Bonferroni z=3.0)
  ```
  Every proposal carries "Verify first: `python backtest.py --days 60` before and after". Nothing writes to `.env`.
- [ ] The report header always prints n, window, config hash, k, breakeven, "Costs not modelled", and "Advisory only".

---

## File-by-file changes

| File | Action | Why |
|---|---|---|
| `alert_log.py` | CREATE | schema, `make_alert_id`, ingest, CSV import, `bucket_stats` (stdlib only) |
| `resolve_alerts.py` | CREATE | ingest + resolve CLI, backfill subcommand |
| `review.py` | CREATE | Feature B |
| `requirements-review.txt` | CREATE | `anthropic` (review host only) |
| `docs/LEARNINGS.md` | CREATE | human-owned constraints and findings |
| `deploy/resolve-alerts.{service,timer}`, `deploy/review.{service,timer}` | CREATE | systemd; same `User=`/paths template as existing units |
| `run_resolver.bat` / `.sh` | CREATE | Windows/macOS equivalents (load `.env`, run once) |
| `outcomes.py` | UPDATE | add `r_multiple(outcome, entry, sl, tp)` |
| `signal_bot.py` | UPDATE | `CONFIG_HASH`, `_alert_context`, kwargs at the existing `append_alert` call, `config_hash` in status. **Nothing else.** |
| `backtest.py` | UPDATE | extra trade columns, `--db`, use `r_multiple` |
| `dashboard/status.py` | UPDATE | DB outcome merge, `load_stats`, `ANTHROPIC_API_KEY` in `SECRET_ENV`, `config_hash` field |
| `dashboard/app.py` | UPDATE | `GET /api/stats` |
| `dashboard/templates/index.html`, `static/app.js`, `static/style.css` | UPDATE | stats card |
| `.env.example` | UPDATE | keys below (placeholders only) |
| `.gitignore` | UPDATE | `reports/`, `trades*.csv` (`.env` and `.alerts_*` already present) |
| `README.md` | UPDATE | Outcome log, Reviewer, Windows scheduling |
| `conftest.py` | UPDATE | isolate the DB path in `tmp_path` |
| `test_alert_log.py`, `test_resolve_alerts.py`, `test_review.py` | CREATE | below |
| `test_signal_bot.py`, `test_dashboard.py`, `test_backtest.py` | UPDATE | below |

### New `.env.example` keys

```bash
# ---- Alert outcome log (resolve_alerts.py) ----
# Unresolved alerts older than this are marked expired and left out of stats.
RESOLVE_MAX_HOLD_DAYS=5
# Buckets with fewer resolved alerts than this are shown as noise.
STATS_MIN_N=30

# ---- Weekly reviewer (review.py, optional) ----
# Only review.py reads this. Never needed by the bot or dashboard.
ANTHROPIC_API_KEY=
REVIEW_MODEL=claude-opus-5-5
REVIEW_EFFORT=high
REVIEW_WINDOW_DAYS=28
REVIEW_MAX_ALERTS=400
# No API call at all below this many resolved live alerts on the current config.
REVIEW_MIN_N=100
REVIEW_BUCKET_MIN_N=30
REVIEW_ALPHA=0.05
REVIEW_REPORTS_DIR=reports
REVIEW_POST_DISCORD=false
```

---

## Test plan (pytest, `class TestX` + `monkeypatch` + `tmp_path`; no network, no real API)

- [ ] `test_alert_log.py`
  - `make_alert_id` is equal for `"2026-10-06 12:00:00+00:00"`, the ISO form, and an aware `Timestamp`.
  - Ingesting twice inserts once.
  - A duplicate `alert_id` with a different `sent_utc` is counted.
  - The truncation-gap warning fires.
  - Legacy CSV `TP/SL` maps to `win/loss`.
  - New CSV columns survive the import.
  - `bucket_stats` matches hand-computed n/mean/SE.
  - `noise` fires below `min_n`.
  - `sig_negative` respects Bonferroni k.
- [ ] `test_resolve_alerts.py`, with fake `fetch` returning crafted frames:
  - win, loss, and the same bar touching both (counts as a loss)
  - open → still open, then win on a re-run
  - a final row is never changed on re-run
  - expired after max hold
  - entry bar missing 3× → unresolvable
  - `fetch` raises `HTTPError`: rows unchanged, exit 1, webhook/token redacted
  - empty frame → no-op
  - `exit_gapped` set when the exit bar's open is beyond the stop
  - `entry_mismatch` flagged
- [ ] `test_signal_bot.py`
  - A sent alert's JSONL row has all the new keys and a correct `stack_bars`/`bars_since_last_alert`.
  - When `_alert_context` raises, the alert is still sent and logged and `run_once` returns `"sent"`.
  - The `run_once` result and Discord call order are unchanged across the existing `TestCooldown` cases.
  - `CONFIG_HASH` changes when `RR` changes and is stable otherwise.
- [ ] `test_dashboard.py`
  - `/api/stats` with no DB returns 200 with a message.
  - Rows with small n are flagged as noise.
  - A DB final outcome replaces `unknown` for an alert older than the bars file.
  - The response has no secret (reuse `SENTINEL`, add `ANTHROPIC_API_KEY`).
- [ ] `test_backtest.py`: trade dicts include the new columns, and `r` equals `outcomes.r_multiple`.
- [ ] `test_review.py`, with a fake client object injected into `call_llm(client=…)`:
  - n < min → no client call, "Insufficient data" report.
  - A valid JSON response renders a report whose numbers come from stats and not the LLM.
  - A proposal citing a non-significant bucket, an unknown key, or a secret key is rejected.
  - Malformed JSON, a refusal, or `max_tokens` give a failure report and exit 1.
  - The API key never appears in the report.
  - `--no-llm` never constructs a client.
  - Running `python -c "import signal_bot, dashboard.app, sys; assert 'review' not in sys.modules"` as a subprocess passes.

```bash
pytest -v
python resolve_alerts.py --dry-run
python review.py --no-llm --dry-run
```

---

## §S Security

- [ ] **Now:** in Discord, go to Server Settings → Integrations → Webhooks and delete the webhook committed in `ac11f0b` (2026-08-13), if it still exists. It is not the one in your current `.env`. Optionally rotate the current one as well and update `.env` on the Pi and Windows.
- [ ] A history rewrite (`git filter-repo`) is optional. It doesn't un-leak anything (forks and clones keep it), so deleting the webhook is the real fix.
- [ ] `.env` is already in `.gitignore` and untracked. Keep it that way.
- [ ] Add `reports/` and `trades*.csv` to `.gitignore`. Trading performance doesn't belong in a public repo, and the CSV is currently one `git add .` from being committed.
- [ ] This plan adds **no** secrets to tracked files. `.env.example` gets empty placeholders only. The DB holds no secrets, and `reason` strings are already `_redact`-ed by `append_alert`.
- [ ] `ANTHROPIC_API_KEY` goes in `SECRET_ENV` (dashboard redaction and the response backstop), and `review.py` errors are passed through `redact` before reaching a report or Discord.
- [ ] The LLM only receives data the bot generated, plus `LEARNINGS.md` and config values from the allowlist. It never receives the webhook, the OANDA token, or the dashboard token.

---

## Implementation order

1. **S:** delete the old webhook and update `.gitignore` (5 min, independent of everything else).
2. **A-core:** `outcomes.r_multiple`, `alert_log.py`, `_alert_context` in the bot, with tests. Deploy the bot, which only adds fields.
3. **A-resolver:** `resolve_alerts.py`, the timer, and tests. Deploy, and check the DB fills and resolves over 24 hours.
4. **A-backfill:** extend `backtest.py` and run it with `--db`. Import the legacy CSV as `backtest:legacy`.
5. **A-dashboard:** DB outcome merge and `/api/stats`.
6. **Gate:** wait for ≥ `REVIEW_MIN_N` (100) resolved live alerts on one `config_hash`. At about 20 a day that is roughly a week, but **every `.env` strategy change resets it**.
7. **B-stats:** `docs/LEARNINGS.md` and `review.py --no-llm` on a timer. This already answers most of the "evidence" goal.
8. **B-LLM:** `call_llm`, the schema, guardrails, and tests. Run by hand twice and read the reports before enabling the timer.
9. **B-Discord:** optional summary post.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Any change to `_poll` alters alert behaviour | Low | One kwargs dict after the send, never raises, and existing `TestCooldown`/run_once tests must pass unchanged |
| Stats mix configs | High without a hash | `config_hash` on every row, and stats default to the current hash |
| Multiple comparisons produce fake "bad buckets" | High | Bonferroni z, `min_n`, k printed, and the LLM can only cite `sig_negative` buckets |
| Live and backtest populations differ (one position, cooldown slot) | Certain | `source` column, never pooled, and backtest shown only as a prior |
| JSONL truncated before ingest (resolver down for over ~25 days) | Low | Gap warning, and the dashboard health banner could show resolver age |
| OANDA rate limits or outages | Medium | Fetch outside the transaction, retry next tick, `attempts` counter |
| −1R understates weekend gap losses | Medium | `exit_gapped` flag, counted in the report |
| Costs not modelled, so the true breakeven is above 40% | Certain | Stated in every report and the stats card. Optional `REVIEW_COST_R` later. |
| Deploy units hardcode `User=pi`, `/home/pi`, but your Pi runs as `finn` | Medium | New units follow the same template. Edit on install, or template all four together. |
| LLM proposes overfit tweaks | Medium | Advisory only, guardrails, and a mandatory backtest command per proposal |

## Open questions

1. What does "gap" mean in the bucket list? I assumed `bars_since_last_alert`. Did you mean bars since a session break?
2. Should `reports/` be gitignored (my default) or committed?
3. Should Discord sends that raised (path (a)) be logged as `send_failed` rows? That needs a small change around the send, so it is deferred unless you want it.
4. Opus 5.5 (default) or Sonnet 5.5 for the reviewer? It's about $1.20 a month against about $0.60.
5. Should `REVIEW_MIN_N=100` and `STATS_MIN_N=30` be lower to start, accepting noisier output?

## What I would NOT build

- Auto-applying any LLM output, or any write path from the reviewer or dashboard to `.env`.
- An LLM anywhere in the alert path, or per-alert LLM commentary.
- The LLM computing statistics. Code computes them, and the LLM only interprets bucket_ids.
- Fixes for duplicate paths (a)–(c) in this change. They alter alert timing; measure first, then decide with the numbers.
- An ORM, a migrations framework, Postgres, or a separate time-series DB.
- Mark-to-market R for expired trades. They are excluded and counted instead.
- Automatic backtests of each proposal on the Pi (60 days of M1 walked bar by bar is slow there). The command is printed for a human to run.
- 24 hourly buckets by default, or ML-based alert filtering.
