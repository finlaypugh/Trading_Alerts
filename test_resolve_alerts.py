"""
Tests for resolve_alerts.py: ingest the bot's alert log, resolve outcomes
against candles, and stay correct when re-run or when OANDA fails.

Run locally:
    pytest test_resolve_alerts.py -v

No network: the candle fetch is a fake serving a hand-written frame.
"""
import json
from datetime import timedelta

import pandas as pd
import pytest
import requests

import alert_log
import resolve_alerts
import signal_bot

# conftest forces 15m bars.
T0 = pd.Timestamp("2026-10-05 10:00", tz="UTC")
STEP = pd.Timedelta(minutes=15)
LATER = (T0 + timedelta(hours=6)).to_pydatetime()


def at(i):
    return T0 + i * STEP


class FakeFetch:
    """Serves candles from `bars` [(open, high, low, close), ...] starting at T0."""

    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def __call__(self, instrument, granularity, start, end, **kwargs):
        self.calls.append((instrument, granularity, start))
        idx = pd.date_range(T0, periods=len(self.bars), freq=STEP)
        df = pd.DataFrame(self.bars, columns=["Open", "High", "Low", "Close"], index=idx)
        return df[df.index >= pd.Timestamp(start)]


def quiet(close=10.0):
    return (close, close + 0.2, close - 0.2, close)


BUY = {"bar_time": str(at(0)), "signal": "BUY", "tier": "STRONG", "depth": 1,
       "price": 10.0, "sl": 9.0, "tp": 11.5, "rr": 1.5, "risk": 1.0,
       "sent_utc": "2026-10-05T10:15:05+00:00"}
SELL = dict(BUY, signal="SELL", sl=11.0, tp=8.5)


@pytest.fixture
def env(tmp_path):
    class Env:
        db = tmp_path / ".alerts_TEST.db"
        alerts = tmp_path / ".alerts_TEST.jsonl"

        def log(self, *records):
            self.alerts.write_text("".join(json.dumps(r) + "\n" for r in records))

        def run(self, bars, now=LATER, **kw):
            self.fetch = bars if isinstance(bars, FakeFetch) else FakeFetch(bars)
            return resolve_alerts.run(fetch=self.fetch, now=now, alerts_file=self.alerts,
                                      path=self.db, **kw)

        def rows(self):
            conn = alert_log.connect(self.db)
            try:
                return alert_log.load_rows(conn, "live")
            finally:
                conn.close()

        def row(self):
            (r,) = self.rows()
            return r

    return Env()


class TestOutcomes:
    def test_target_hit_is_a_win(self, env):
        env.log(BUY)
        assert env.run([quiet(), quiet(), (10.0, 11.6, 9.9, 11.4)]) == 0
        r = env.row()
        assert (r["outcome"], r["r"], r["bars_held"]) == ("win", 1.5, 2)
        assert r["exit_time"] == at(2).isoformat()
        assert r["exit_gapped"] == 0

    def test_stop_hit_is_a_loss(self, env):
        env.log(BUY)
        env.run([quiet(), (10.0, 10.1, 8.9, 9.2)])
        assert (env.row()["outcome"], env.row()["r"]) == ("loss", -1.0)

    def test_one_bar_spanning_both_levels_is_a_loss(self, env):
        env.log(BUY)
        env.run([quiet(), (10.0, 12.0, 8.0, 10.0)])
        assert env.row()["outcome"] == "loss"

    def test_sell_is_mirrored(self, env):
        env.log(SELL)
        env.run([quiet(), (10.0, 10.1, 8.4, 8.6)])
        assert (env.row()["outcome"], env.row()["r"]) == ("win", 1.5)

    def test_the_entry_bar_itself_does_not_count(self, env):
        env.log(BUY)
        env.run([(10.0, 12.0, 9.9, 10.0), quiet()])
        assert env.row()["outcome"] == "open"

    def test_a_gap_through_the_stop_is_flagged(self, env):
        env.log(BUY)
        env.run([quiet(), (8.5, 8.7, 8.3, 8.6)])
        r = env.row()
        assert (r["outcome"], r["r"], r["exit_gapped"]) == ("loss", -1.0, 1)

    def test_entry_that_disagrees_with_the_candle_is_flagged_but_resolved(self, env):
        env.log(dict(BUY, price=10.5, sl=9.5, tp=12.0))
        env.run([quiet(10.0), (10.0, 12.1, 9.9, 12.0)])
        r = env.row()
        assert (r["outcome"], r["last_error"]) == ("win", "entry_mismatch")

    def test_records_how_far_it_has_looked(self, env):
        env.log(BUY)
        env.run([quiet(), quiet(), quiet()])
        assert env.row()["resolved_through"] == at(2).isoformat()


class TestReruns:
    def test_open_then_closed_on_a_later_run(self, env):
        env.log(BUY)
        env.run([quiet(), quiet()])
        assert env.row()["outcome"] == "open"
        env.run([quiet(), quiet(), (10.0, 11.6, 9.9, 11.0)])
        assert env.row()["outcome"] == "win"

    def test_a_closed_alert_is_never_rewritten(self, env):
        env.log(BUY)
        env.run([quiet(), (10.0, 11.6, 9.9, 11.0)])
        # Different candles on a later run cannot change a settled result.
        env.run([quiet(), (10.0, 10.1, 8.0, 8.5)])
        assert env.row()["outcome"] == "win"
        assert env.fetch.calls == []

    def test_rerunning_is_a_no_op(self, env, capsys):
        env.log(BUY, SELL)
        env.run([quiet(), (10.0, 11.6, 9.9, 11.0)])
        before = env.rows()
        env.run([quiet(), (10.0, 11.6, 9.9, 11.0)])
        assert env.rows() == before
        assert "ingested 0 new" in capsys.readouterr().out

    def test_fetch_starts_at_the_oldest_unresolved_alert(self, env):
        env.log(BUY, dict(BUY, bar_time=str(at(3))))
        env.run([quiet()] * 5)
        assert [c[2] for c in env.fetch.calls] == [at(0).isoformat()]
        assert env.fetch.calls[0][:2] == ("TESTTICKER", "M15")

    def test_open_too_long_expires(self, env):
        env.log(BUY)
        env.run([quiet(), quiet()], now=(T0 + timedelta(days=30)).to_pydatetime())
        assert env.row()["outcome"] == "expired"

    def test_missing_entry_bar_becomes_unresolvable_after_three_runs(self, env):
        env.log(dict(BUY, bar_time=str(at(0) + pd.Timedelta(minutes=7))))
        for expected in ("pending", "pending", "unresolvable"):
            env.run([quiet(), quiet()])
            r = env.row()
            assert r["outcome"] == expected
        assert (r["attempts"], r["last_error"]) == (3, "entry_bar_missing")

    def test_missing_levels_are_unresolvable(self, env):
        env.log({k: v for k, v in BUY.items() if k != "sl"})
        env.run([quiet(), quiet()])
        assert (env.row()["outcome"], env.row()["last_error"]) == ("unresolvable", "no_levels")


class TestFailures:
    def test_fetch_error_changes_nothing_but_keeps_the_ingest(self, env, monkeypatch, capsys):
        token = "secret-oanda-token-123"
        monkeypatch.setenv("OANDA_API_TOKEN", token)

        def boom(*a, **k):
            raise requests.HTTPError(
                f"OANDA 503 token {token} webhook {signal_bot.DISCORD_WEBHOOK_URL}"
            )

        env.log(BUY)
        assert resolve_alerts.run(fetch=boom, now=LATER, alerts_file=env.alerts,
                                  path=env.db) == 1
        assert env.row()["outcome"] == "pending"
        out = capsys.readouterr().out
        assert "resolve failed, nothing changed" in out
        assert token not in out
        assert signal_bot.DISCORD_WEBHOOK_URL not in out

    def test_empty_frame_is_a_no_op(self, env):
        env.log(BUY)
        assert env.run([]) == 0
        assert (env.row()["outcome"], env.row()["attempts"]) == ("pending", 0)

    def test_dry_run_writes_nothing(self, env):
        env.log(BUY)
        env.run([quiet(), (10.0, 11.6, 9.9, 11.0)], dry_run=True)
        assert env.rows() == []

    def test_missing_alert_file_is_fine(self, env):
        assert env.run([quiet()]) == 0
        assert env.rows() == []


class TestMain:
    def test_backfill_refuses_a_source_that_could_pass_as_live(self, tmp_path):
        assert resolve_alerts.main(["backfill", str(tmp_path / "t.csv"), "--source", "live"]) == 2

    def test_backfill_imports_into_its_own_source(self, tmp_path):
        csv_path = tmp_path / "t.csv"
        csv_path.write_text(
            "entry_time,signal,entry,sl,tp,outcome,r\n"
            "2026-08-02 22:28:00+00:00,BUY,10,9,11.5,TP,1.5\n"
        )
        db = tmp_path / "a.db"
        assert resolve_alerts.backfill(csv_path, "backtest:legacy", "XAU_USD", "1m", db=db) == 0
        conn = alert_log.connect(db)
        (row,) = alert_log.load_rows(conn, "backtest:legacy")
        conn.close()
        assert (row["outcome"], row["ticker"]) == ("win", "XAU_USD")

    def test_needs_an_oanda_token_to_resolve(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "OANDA_API_TOKEN", "")
        assert resolve_alerts.main([]) == 2
