#!/usr/bin/env python3
"""
Copy the bot's sent alerts into the durable alert log and work out how each
one ended, against OANDA candles.

    python resolve_alerts.py              # ingest + resolve: what the timer runs
    python resolve_alerts.py --dry-run    # report what would change, write nothing
    python resolve_alerts.py backfill trades.csv --source backtest:legacy

The bot only keeps its newest ALERTS_KEEP alerts in .alerts_<ticker>.jsonl and
the dashboard can only judge alerts its bars file still covers. This keeps
every alert, with its outcome, in .alerts_<ticker>.db. It is the only thing
that writes there, and it never touches the bot's own files.

Outcomes use the backtest's rule (outcomes.resolve): walk forward from the bar
after the alert, and when one bar spans both the stop and the target, count
the loss. Re-running is safe: closed alerts are never rewritten, and open ones
are walked again from their own bar each run, so a run that fails part way
leaves nothing half-done for the next to trip over.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import alert_log
import outcomes
import signal_bot

ROOT = Path(__file__).resolve().parent

# An alert still open this long after its bar is given up on and left out of
# the statistics; no mark-to-market price is invented for it.
MAX_HOLD_DAYS = float(os.environ.get("RESOLVE_MAX_HOLD_DAYS", 5))
# Runs in a row an alert's own bar can be missing from OANDA before it is
# marked unresolvable.
MAX_ATTEMPTS = 3
# The candle the bot alerted on is final; a refetch that disagrees on its
# close by more than this is flagged.
ENTRY_TOLERANCE = 0.01


def db_path():
    return alert_log.db_path(ROOT, signal_bot.TICKER)


def redact(text):
    """The bot's webhook redaction, plus the OANDA token."""
    text = signal_bot._redact(text)
    token = os.environ.get("OANDA_API_TOKEN", "")
    return text.replace(token, "<token>") if len(token) >= 8 else text


def _gapped(signal, outcome, bar_open, sl, tp):
    """Did the exit bar open already beyond the level? Then the real fill was worse."""
    if outcome == "loss":
        return bar_open <= sl if signal == "BUY" else bar_open >= sl
    return bar_open >= tp if signal == "BUY" else bar_open <= tp


def _update(row, **fields):
    return {"source": row["source"], "alert_id": row["alert_id"], **fields}


def resolve_pending(conn, fetch=None, now=None, max_hold=None):
    """
    Work out every unresolved live alert. Returns the updates to apply, one
    dict per alert; nothing is written here. Raises whatever the fetch raises,
    before anything is decided, so a failed fetch changes nothing.
    """
    fetch = fetch or signal_bot.fetch_candles
    now = now or datetime.now(timezone.utc)
    max_hold = timedelta(days=MAX_HOLD_DAYS) if max_hold is None else max_hold
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM alerts WHERE source = 'live' AND outcome IN ('pending', 'open') "
        "ORDER BY bar_time"
    )]
    groups = {}
    for r in rows:
        groups.setdefault((r["ticker"], r["interval"]), []).append(r)

    updates = []
    stamp = now.isoformat(timespec="seconds")
    for (ticker, interval), grp in groups.items():
        start = min(r["bar_time"] for r in grp)
        df = fetch(ticker, signal_bot.oanda_granularity(interval), start, now)
        if df.empty:
            continue
        opens, highs, lows, closes = (
            df[c].to_numpy(dtype=float) for c in ("Open", "High", "Low", "Close")
        )
        pos = {alert_log.iso_utc(t): i for i, t in enumerate(df.index)}
        last_bar = alert_log.iso_utc(df.index[-1])

        for r in grp:
            i = pos.get(r["bar_time"])
            if i is None:
                attempts = r["attempts"] + 1
                updates.append(_update(
                    r, outcome="unresolvable" if attempts >= MAX_ATTEMPTS else r["outcome"],
                    attempts=attempts, last_error="entry_bar_missing", updated_utc=stamp,
                ))
                continue
            if None in (r["entry"], r["sl"], r["tp"]) or r["entry"] == r["sl"]:
                updates.append(_update(
                    r, outcome="unresolvable", attempts=r["attempts"] + 1,
                    last_error="no_levels", updated_utc=stamp,
                ))
                continue

            mismatch = abs(closes[i] - r["entry"]) > ENTRY_TOLERANCE
            error = "entry_mismatch" if mismatch else None
            outcome, j = outcomes.resolve(highs, lows, i, r["signal"], r["sl"], r["tp"])
            if outcome == "open":
                opened = datetime.fromisoformat(r["bar_time"])
                expired = now - opened > max_hold
                updates.append(_update(
                    r, outcome="expired" if expired else "open",
                    resolved_through=last_bar, last_error=error, updated_utc=stamp,
                ))
                continue
            updates.append(_update(
                r, outcome=outcome,
                exit_time=alert_log.iso_utc(df.index[j]),
                r=outcomes.r_multiple(outcome, r["entry"], r["sl"], r["tp"]),
                bars_held=j - i,
                exit_gapped=int(_gapped(r["signal"], outcome, opens[j], r["sl"], r["tp"])),
                resolved_through=last_bar, last_error=error, updated_utc=stamp,
            ))
    return updates


def apply_updates(conn, updates):
    """
    Write resolve_pending's updates. Only rows still pending or open are
    touched, so an alert that has closed keeps its result whatever later runs
    see. Does not commit. Returns rows changed.
    """
    changed = 0
    for u in updates:
        fields = {k: v for k, v in u.items() if k not in ("source", "alert_id")}
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        cur = conn.execute(
            f"UPDATE alerts SET {sets} WHERE source = :source AND alert_id = :alert_id "
            "AND outcome IN ('pending', 'open')",
            u,
        )
        changed += cur.rowcount
    return changed


def run(dry_run=False, fetch=None, now=None, alerts_file=None, path=None):
    """Ingest, then resolve. Returns an exit code: 0 ok, 1 the resolve step failed."""
    conn = alert_log.connect(path or db_path())
    try:
        ingested = alert_log.ingest_jsonl(
            conn, alerts_file or signal_bot.ALERTS_FILE,
            signal_bot.TICKER, signal_bot.INTERVAL, keep=signal_bot.ALERTS_KEEP,
        )
        if ingested["gap"]:
            print("WARNING: alert log gap: the bot's alert file was truncated past the "
                  "newest logged alert, so some alerts were never recorded. Run the "
                  "resolver more often than the bot fills ALERTS_KEEP lines.")
        print(f"ingested {ingested['inserted']} new of {ingested['read']} lines "
              f"({ingested['skipped']} skipped, {ingested['duplicates']} duplicate sends)")
        # Committed before the network call, so a failed fetch still keeps them.
        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        try:
            updates = resolve_pending(conn, fetch=fetch, now=now)
        except (requests.RequestException, ValueError) as exc:
            print(f"resolve failed, nothing changed: {redact(f'{type(exc).__name__}: {exc}')}")
            return 1

        counts = {}
        for u in updates:
            counts[u["outcome"]] = counts.get(u["outcome"], 0) + 1
        changed = apply_updates(conn, updates)
        if dry_run:
            conn.rollback()
            print(f"dry run: would update {changed} alerts {counts}")
        else:
            conn.commit()
            print(f"updated {changed} alerts {counts}")
        return 0
    finally:
        conn.close()


def backfill(path, source, ticker, interval, db=None):
    conn = alert_log.connect(db or db_path())
    try:
        added = alert_log.import_trades_csv(conn, path, source, ticker, interval)
        conn.commit()
    finally:
        conn.close()
    print(f"imported {added} trades from {path} as {source}")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    sub = p.add_subparsers(dest="command")
    b = sub.add_parser("backfill", help="import a backtest trades.csv")
    b.add_argument("csv")
    b.add_argument("--source", default="backtest:legacy",
                   help="label kept apart from live alerts (default backtest:legacy)")
    b.add_argument("--ticker", default=None, help="default: SIGNAL_TICKER")
    b.add_argument("--interval", default="1m", help="bar size the backtest ran on (default 1m)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "backfill":
        if not args.source.startswith("backtest:"):
            print("--source must start with backtest: so it is never mistaken for live alerts",
                  file=sys.stderr)
            return 2
        return backfill(args.csv, args.source, args.ticker or signal_bot.TICKER, args.interval)
    if not signal_bot.TICKER:
        print("SIGNAL_TICKER is not set.", file=sys.stderr)
        return 2
    if not signal_bot.OANDA_API_TOKEN:
        print("OANDA_API_TOKEN is not set: alerts can be ingested but not resolved.",
              file=sys.stderr)
        return 2
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
