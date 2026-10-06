"""
Durable alert and outcome log, in SQLite, plus the bucket statistics read
from it.

The bot never opens the database. It keeps writing its capped
.alerts_<ticker>.jsonl; resolve_alerts.py copies those lines in here, works
out how each alert ended, and is the only writer. The dashboard and review.py
open it read-only.

No project imports on purpose, like outcomes.py: signal_bot, backtest, the
dashboard and review.py all import this module, and it must not drag any of
them into the others.
"""
import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, fmean, stdev

COLUMNS = (
    "source", "alert_id", "ticker", "interval", "config_hash",
    "bar_time", "sent_utc", "signal", "tier", "depth", "strength",
    "entry", "sl", "tp", "rr", "risk", "atr", "ema_fast", "ema_mid", "ema_slow",
    "stack_bars", "bars_since_last_alert", "reason",
    "outcome", "exit_time", "r", "bars_held", "exit_gapped",
)

DDL = """
CREATE TABLE IF NOT EXISTS alerts (
  source TEXT NOT NULL,                 -- 'live' | 'backtest:<run>' | 'backtest:legacy'
  alert_id TEXT NOT NULL,               -- make_alert_id(ticker, signal, bar_time)
  ticker TEXT, interval TEXT, config_hash TEXT,
  bar_time TEXT NOT NULL,               -- ISO-8601 UTC; entry is this bar's close
  sent_utc TEXT, signal TEXT NOT NULL, tier TEXT, depth INTEGER, strength REAL,
  entry REAL, sl REAL, tp REAL, rr REAL, risk REAL, atr REAL,
  ema_fast REAL, ema_mid REAL, ema_slow REAL,
  stack_bars INTEGER, bars_since_last_alert INTEGER, reason TEXT,
  outcome TEXT NOT NULL DEFAULT 'pending',  -- pending|open|win|loss|expired|unresolvable
  exit_time TEXT, r REAL, bars_held INTEGER, exit_gapped INTEGER,
  duplicate_sends INTEGER NOT NULL DEFAULT 0,
  resolved_through TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  updated_utc TEXT,
  PRIMARY KEY (source, alert_id)
);
CREATE INDEX IF NOT EXISTS ix_alerts_unresolved ON alerts(outcome)
  WHERE outcome IN ('pending', 'open');
"""

OUTCOMES = ("pending", "open", "win", "loss", "expired", "unresolvable")
CLOSED = ("win", "loss")
UNRESOLVED = ("pending", "open")
# Older backtest.py versions wrote the level that was hit, not the result.
LEGACY_OUTCOMES = {"TP": "win", "SL": "loss"}


# ---------------------------------------------------------------------------
# Identity and parsing
# ---------------------------------------------------------------------------

def iso_utc(value):
    """ISO-8601 UTC text for any timestamp-like, naive read as UTC. None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):  # pandas Timestamps included
        ts = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None
    ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
    return ts.isoformat()


def make_alert_id(ticker, signal, bar_time):
    """
    Stable across the bot, the backtest and a backfill: the same bar gives
    the same id whichever way its time was spelled.
    """
    return f"{ticker}|{signal}|{iso_utc(bar_time)}"


def db_path(root, ticker):
    return Path(root) / f".alerts_{str(ticker).replace('/', '_')}.db"


def _num(value):
    """float, or None for blank / NaN / inf / garbage. CSV hands everything over as text."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _int(value):
    x = _num(value)
    return None if x is None else int(x)


def normalise(rec, source, ticker=None, interval=None):
    """
    One alert as a DB row, from a bot JSONL record, a backtest trade dict or a
    trades.csv row. None when it has no usable signal or bar time.
    """
    signal = rec.get("signal")
    bar_time = iso_utc(rec.get("bar_time") or rec.get("entry_time"))
    if signal not in ("BUY", "SELL") or bar_time is None:
        return None
    ticker = rec.get("ticker") or ticker
    outcome = LEGACY_OUTCOMES.get(rec.get("outcome"), rec.get("outcome")) or "pending"
    if outcome not in OUTCOMES:
        outcome = "pending"
    closed = outcome in CLOSED
    entry = rec.get("entry")
    return {
        "source": source,
        "alert_id": make_alert_id(ticker, signal, bar_time),
        "ticker": ticker,
        "interval": rec.get("interval") or interval,
        "config_hash": rec.get("config_hash") or None,
        "bar_time": bar_time,
        "sent_utc": rec.get("sent_utc") or None,
        "signal": signal,
        "tier": rec.get("tier") or None,
        "depth": _int(rec.get("depth")),
        "strength": _num(rec.get("strength")),
        # The bot logs the entry as "price"; the backtest calls it "entry".
        "entry": _num(rec.get("price") if entry in (None, "") else entry),
        "sl": _num(rec.get("sl")),
        "tp": _num(rec.get("tp")),
        "rr": _num(rec.get("rr")),
        "risk": _num(rec.get("risk")),
        "atr": _num(rec.get("atr")),
        "ema_fast": _num(rec.get("ema_fast")),
        "ema_mid": _num(rec.get("ema_mid")),
        "ema_slow": _num(rec.get("ema_slow")),
        "stack_bars": _int(rec.get("stack_bars")),
        "bars_since_last_alert": _int(rec.get("bars_since_last_alert")),
        "reason": rec.get("reason") or None,
        "outcome": outcome,
        "exit_time": iso_utc(rec.get("exit_time")) if closed else None,
        "r": _num(rec.get("r")) if closed else None,
        "bars_held": _int(rec.get("bars_held")) if closed else None,
        "exit_gapped": None,
    }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect(path, readonly=False):
    """
    A connection with rows as sqlite3.Row. Read-only connections never create
    the file and raise FileNotFoundError when it is missing. The writer gets
    WAL, so readers never block it, and FULL sync, since a Pi loses power.
    """
    path = Path(path)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(DDL)
    conn.row_factory = sqlite3.Row
    return conn


def insert(conn, rows):
    """INSERT OR IGNORE: a row already logged keeps what it has. Returns rows added."""
    rows = list(rows)
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO alerts ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join(':' + c for c in COLUMNS)})",
        rows,
    )
    return conn.total_changes - before


def ingest_jsonl(conn, path, ticker, interval, keep=None):
    """
    Copy the bot's alert log into the DB. Safe to repeat: a line already in
    the DB is ignored. Does not commit.

    Returns counts: read, inserted, skipped (unparseable or unusable lines),
    duplicates (alerts the file holds more than once, under different send
    times -- the same bar alerted twice), and gap, true when the file was
    already truncated past the newest logged alert, so alerts between the two
    were lost before they could be copied.
    """
    result = {"read": 0, "inserted": 0, "skipped": 0, "duplicates": 0, "gap": False}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result

    newest = conn.execute(
        "SELECT MAX(bar_time) FROM alerts WHERE source = 'live'"
    ).fetchone()[0]

    rows, sends = [], {}
    for line in lines:
        if not line.strip():
            continue
        result["read"] += 1
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        row = normalise(rec, "live", ticker, interval) if isinstance(rec, dict) else None
        if row is None:
            result["skipped"] += 1
            continue
        rows.append(row)
        sends.setdefault(row["alert_id"], set()).add(row["sent_utc"])

    if (newest is not None and keep and result["read"] >= keep and rows
            and min(r["bar_time"] for r in rows) > newest):
        result["gap"] = True

    result["inserted"] = insert(conn, rows)
    for alert_id, sent in sends.items():
        extra = len(sent) - 1
        if extra > 0:
            cur = conn.execute(
                "UPDATE alerts SET duplicate_sends = ? WHERE source = 'live' "
                "AND alert_id = ? AND duplicate_sends < ?",
                (extra, alert_id, extra),
            )
            result["duplicates"] += cur.rowcount
    return result


def import_trades(conn, trades, source, ticker, interval):
    """Backtest trades (dicts, or rows of a trades.csv) into the DB. Does not commit."""
    rows = [normalise(t, source, ticker, interval) for t in trades]
    return insert(conn, (r for r in rows if r is not None))


def import_trades_csv(conn, path, source, ticker, interval):
    """
    A trades.csv from any backtest.py version, current (outcome win/loss/open)
    or legacy (outcome TP/SL, no bars_held). Columns it lacks stay NULL.
    """
    with open(path, newline="", encoding="utf-8") as f:
        return import_trades(conn, csv.DictReader(f), source, ticker, interval)


def latest_backtest_source(conn):
    row = conn.execute(
        "SELECT source FROM alerts WHERE source LIKE 'backtest:%' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def load_rows(conn, source="live", config_hash=None):
    """Every alert of one source, oldest first, as plain dicts. All outcomes included."""
    sql, args = "SELECT * FROM alerts WHERE source = ?", [source]
    if config_hash:
        sql += " AND config_hash = ?"
        args.append(config_hash)
    return [dict(r) for r in conn.execute(sql + " ORDER BY bar_time", args)]


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

# UTC hours. Gold trades round the clock with a daily break near 21:00-22:00.
SESSIONS = (("Asia", 22, 7), ("London", 7, 12), ("NY", 12, 17), ("Late", 17, 22))


def session_of(hour):
    for name, start, end in SESSIONS:
        if (start <= hour < end) if start < end else (hour >= start or hour < end):
            return name
    return "unknown"


def _bin(value, edges, labels, missing):
    if value is None:
        return missing
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


def annotate(rows):
    """
    Add the derived fields buckets are cut on. `rows` must be one source,
    oldest first, with every outcome present: whether an earlier alert was
    still open needs the alerts that have not closed yet too.

      prev_same_dir  how the previous alert in the same direction stood at
                     this alert's bar: win, loss, open (not exited yet),
                     unknown (expired or unresolvable) or none
      overlaps_open  an earlier alert in either direction had not exited yet
    """
    out, last_by_side, exits = [], {}, []
    for row in rows:
        a = dict(row)
        t = a["bar_time"]
        a["hour_utc"] = datetime.fromisoformat(t).hour
        a["session"] = session_of(a["hour_utc"])

        prev = last_by_side.get(a["signal"])
        if prev is None:
            a["prev_same_dir"] = "none"
        elif prev["outcome"] in CLOSED and prev["exit_time"] and prev["exit_time"] <= t:
            a["prev_same_dir"] = prev["outcome"]
        elif prev["outcome"] in CLOSED + UNRESOLVED:
            a["prev_same_dir"] = "open"
        else:
            a["prev_same_dir"] = "unknown"

        # ISO strings in one format compare in time order. None = still open.
        exits = [e for e in exits if e is None or e > t]
        a["overlaps_open"] = "yes" if exits else "no"
        if a["outcome"] in CLOSED and a["exit_time"]:
            exits.append(a["exit_time"])
        elif a["outcome"] in UNRESOLVED:
            exits.append(None)

        last_by_side[a["signal"]] = a
        out.append(a)
    return out


# dimension -> (key function over an annotated row, display order of its buckets)
DIMENSIONS = {
    "signal": (lambda a: a["signal"], ("BUY", "SELL")),
    "depth": (lambda a: a["depth"], (0, 1, 2)),
    "tier": (lambda a: a["tier"], ("STRONG", "WEAK")),
    "session": (lambda a: a["session"], tuple(s[0] for s in SESSIONS)),
    "since_last": (
        lambda a: _bin(a["bars_since_last_alert"], (4, 10, 30), ("0-3", "4-9", "10-29", "30+"), "none"),
        ("none", "0-3", "4-9", "10-29", "30+"),
    ),
    "stack_bars": (
        lambda a: _bin(a["stack_bars"], (10, 50), ("<10", "10-49", "50+"), "unknown"),
        ("<10", "10-49", "50+", "unknown"),
    ),
    "strength": (
        lambda a: _bin(a["strength"], (0.6, 0.8), ("<0.6", "0.6-0.8", "0.8+"), "unknown"),
        ("<0.6", "0.6-0.8", "0.8+", "unknown"),
    ),
    "prev_same_dir": (lambda a: a["prev_same_dir"], ("none", "win", "loss", "open", "unknown")),
    "overlaps_open": (lambda a: a["overlaps_open"], ("no", "yes")),
}


def _bucket(dim, key, rs, min_n, z):
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    p = wins / n
    mean = fmean(rs)
    se = stdev(rs) / math.sqrt(n) if n > 1 else None
    noise = n < min_n
    return {
        "dim": dim,
        "bucket": key,
        "bucket_id": f"{dim}={key}",
        "n": n,
        "wins": wins,
        "win_rate": round(p, 4),
        "win_rate_se": round(math.sqrt(p * (1 - p) / n), 4),
        "mean_r": round(mean, 4),
        "se_r": None if se is None else round(se, 4),
        "net_r": round(sum(rs), 2),
        "noise": noise,
        "sig_negative": bool(not noise and se is not None and mean + z * se < 0),
    }


def bucket_stats(rows, dims, min_n=30, alpha=0.05, rr=1.5):
    """
    Win rate and mean R per bucket of each dimension, over closed trades.

    `rows` are annotate()d. Significance is Bonferroni-corrected across every
    bucket in the result: with k buckets, z is the two-sided critical value
    at alpha / k, and a bucket is sig_negative only when it has at least
    min_n trades and mean_r + z * se_r is still below zero. Anything under
    min_n is flagged as noise however it looks.
    """
    closed = [a for a in rows if a["outcome"] in CLOSED and a["r"] is not None]
    groups = []
    for dim in dims:
        key_fn, order = DIMENSIONS[dim]
        by = {}
        for a in closed:
            by.setdefault(key_fn(a), []).append(a["r"])
        rank = {k: i for i, k in enumerate(order)}
        for key in sorted(by, key=lambda k: (rank.get(k, len(rank)), str(k))):
            groups.append((dim, key, by[key]))

    k = max(1, len(groups))
    z = NormalDist().inv_cdf(1 - alpha / (2 * k))
    all_r = [a["r"] for a in closed]
    return {
        "n": len(closed),
        "k": k,
        "z": round(z, 3),
        "alpha": alpha,
        "min_n": min_n,
        "rr": rr,
        "breakeven_win_rate": round(1 / (1 + rr), 4),
        "overall": _bucket("all", "all", all_r, min_n, NormalDist().inv_cdf(1 - alpha / 2))
        if all_r else None,
        "buckets": [_bucket(dim, key, rs, min_n, z) for dim, key, rs in groups],
    }
