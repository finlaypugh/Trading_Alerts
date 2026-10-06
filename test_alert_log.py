"""
Tests for alert_log.py: alert identity, ingesting the bot's JSONL, importing
backtest CSVs, and the bucket statistics.

Run locally:
    pytest test_alert_log.py -v

SQLite files live in tmp_path. No network.
"""
import json
import math
from datetime import datetime, timezone

import pandas as pd
import pytest

import alert_log


@pytest.fixture
def conn(tmp_path):
    c = alert_log.connect(tmp_path / ".alerts_TEST.db")
    yield c
    c.close()


def jsonl(path, *records):
    path.write_text("".join(
        (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records
    ))
    return path


ALERT = {
    "sent_utc": "2026-10-05T12:00:01+00:00", "bar_time": "2026-10-05 11:59:00+00:00",
    "signal": "BUY", "tier": "STRONG", "depth": 1, "strength": 0.8,
    "price": 10.0, "sl": 9.0, "tp": 11.5, "rr": 1.5, "risk": 1.0, "reason": "r",
}


def rows(conn, source="live"):
    return alert_log.load_rows(conn, source)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class TestAlertId:
    @pytest.mark.parametrize("spelling", [
        "2026-10-05 11:59:00+00:00",
        "2026-10-05T11:59:00+00:00",
        "2026-10-05T11:59:00Z",
        "2026-10-05 11:59:00",                    # naive is UTC
        "2026-10-05T12:59:00+01:00",              # same instant, another zone
        pd.Timestamp("2026-10-05 11:59", tz="UTC"),
        datetime(2026, 10, 5, 11, 59, tzinfo=timezone.utc),
    ])
    def test_every_spelling_of_one_bar_gives_one_id(self, spelling):
        assert alert_log.make_alert_id("XAU_USD", "BUY", spelling) == \
            "XAU_USD|BUY|2026-10-05T11:59:00+00:00"

    def test_direction_is_part_of_the_id(self):
        t = "2026-10-05 11:59:00+00:00"
        assert alert_log.make_alert_id("X", "BUY", t) != alert_log.make_alert_id("X", "SELL", t)

    @pytest.mark.parametrize("value", [None, "", "yesterday"])
    def test_unparseable_time_is_none(self, value):
        assert alert_log.iso_utc(value) is None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

class TestIngestJsonl:
    def test_missing_file_is_a_no_op(self, conn, tmp_path):
        result = alert_log.ingest_jsonl(conn, tmp_path / "nope.jsonl", "T", "1m")
        assert result["read"] == 0
        assert rows(conn) == []

    def test_maps_price_to_entry_and_fills_ticker_and_interval(self, conn, tmp_path):
        alert_log.ingest_jsonl(conn, jsonl(tmp_path / "a.jsonl", ALERT), "XAU_USD", "1m")
        (row,) = rows(conn)
        assert row["entry"] == 10.0
        assert row["ticker"] == "XAU_USD"
        assert row["interval"] == "1m"
        assert row["bar_time"] == "2026-10-05T11:59:00+00:00"
        assert row["alert_id"] == "XAU_USD|BUY|2026-10-05T11:59:00+00:00"
        assert row["outcome"] == "pending"

    def test_fields_the_bot_logs_win_over_defaults(self, conn, tmp_path):
        rec = dict(ALERT, ticker="EUR_USD", interval="5m", atr=2.5, stack_bars=12,
                   bars_since_last_alert=7, config_hash="abc")
        alert_log.ingest_jsonl(conn, jsonl(tmp_path / "a.jsonl", rec), "XAU_USD", "1m")
        (row,) = rows(conn)
        assert (row["ticker"], row["interval"], row["atr"], row["stack_bars"],
                row["bars_since_last_alert"], row["config_hash"]) == \
            ("EUR_USD", "5m", 2.5, 12, 7, "abc")

    def test_ingesting_twice_inserts_once(self, conn, tmp_path):
        path = jsonl(tmp_path / "a.jsonl", ALERT)
        assert alert_log.ingest_jsonl(conn, path, "T", "1m")["inserted"] == 1
        assert alert_log.ingest_jsonl(conn, path, "T", "1m")["inserted"] == 0
        assert len(rows(conn)) == 1

    def test_an_existing_row_keeps_its_outcome(self, conn, tmp_path):
        path = jsonl(tmp_path / "a.jsonl", ALERT)
        alert_log.ingest_jsonl(conn, path, "T", "1m")
        conn.execute("UPDATE alerts SET outcome = 'win', r = 1.5")
        alert_log.ingest_jsonl(conn, path, "T", "1m")
        assert rows(conn)[0]["outcome"] == "win"

    def test_bad_lines_are_skipped_and_counted(self, conn, tmp_path):
        path = jsonl(tmp_path / "a.jsonl", ALERT, "{torn", "[1]",
                     dict(ALERT, signal="HOLD"), dict(ALERT, bar_time="soon"))
        result = alert_log.ingest_jsonl(conn, path, "T", "1m")
        assert (result["read"], result["inserted"], result["skipped"]) == (5, 1, 4)

    def test_the_same_bar_sent_twice_is_counted_once_and_idempotently(self, conn, tmp_path):
        path = jsonl(tmp_path / "a.jsonl", ALERT, dict(ALERT, sent_utc="2026-10-05T12:00:31+00:00"))
        assert alert_log.ingest_jsonl(conn, path, "T", "1m")["duplicates"] == 1
        assert alert_log.ingest_jsonl(conn, path, "T", "1m")["duplicates"] == 0
        assert rows(conn)[0]["duplicate_sends"] == 1

    def test_gap_when_a_full_file_starts_after_the_newest_logged_alert(self, conn, tmp_path):
        alert_log.ingest_jsonl(conn, jsonl(tmp_path / "a.jsonl", ALERT), "T", "1m")
        later = [dict(ALERT, bar_time=f"2026-10-06 0{i}:00:00+00:00") for i in range(3)]
        result = alert_log.ingest_jsonl(conn, jsonl(tmp_path / "b.jsonl", *later), "T", "1m", keep=3)
        assert result["gap"] is True

    def test_no_gap_while_the_file_is_below_its_cap(self, conn, tmp_path):
        alert_log.ingest_jsonl(conn, jsonl(tmp_path / "a.jsonl", ALERT), "T", "1m")
        later = [dict(ALERT, bar_time=f"2026-10-06 0{i}:00:00+00:00") for i in range(2)]
        result = alert_log.ingest_jsonl(conn, jsonl(tmp_path / "b.jsonl", *later), "T", "1m", keep=3)
        assert result["gap"] is False


# ---------------------------------------------------------------------------
# Backtest import
# ---------------------------------------------------------------------------

LEGACY_CSV = (
    "signal,tier,depth,strength,reason,entry_time,entry,sl,tp,rr,risk,exit_time,outcome,r\n"
    'BUY,STRONG,1,0.83,"a, b",2026-08-02 22:28:00+00:00,4067.1,4062.17,4074.49,1.5,4.93,'
    "2026-08-02 23:56:00+00:00,TP,1.5\n"
    "BUY,STRONG,2,0.63,x,2026-08-03 00:15:00+00:00,4072.58,4069.08,4077.84,1.5,3.5,"
    "2026-08-03 00:20:00+00:00,SL,-1.0\n"
)

CURRENT_CSV = (
    "entry_time,exit_time,signal,tier,depth,strength,entry,sl,tp,risk,outcome,r,bars_held,"
    "rr,atr,ema_fast,ema_mid,ema_slow,stack_bars,bars_since_last_alert,config_hash\n"
    "2026-09-01 10:00:00+00:00,2026-09-01 10:07:00+00:00,SELL,STRONG,1,0.7,10.0,11.0,8.5,1.0,"
    "loss,-1.0,7,1.5,2.0,9.0,9.5,10.0,25,,abc123\n"
    "2026-09-01 11:00:00+00:00,,BUY,STRONG,1,0.7,10.0,9.0,11.5,1.0,open,0.0,,1.5,2.0,"
    "11.0,10.5,10.0,4,60,abc123\n"
)


class TestImportTrades:
    def test_legacy_tp_sl_outcomes_become_win_and_loss(self, conn, tmp_path):
        path = tmp_path / "trades.csv"
        path.write_text(LEGACY_CSV)
        assert alert_log.import_trades_csv(conn, path, "backtest:legacy", "XAU_USD", "1m") == 2
        win, loss = rows(conn, "backtest:legacy")
        assert (win["outcome"], win["r"], win["exit_time"]) == \
            ("win", 1.5, "2026-08-02T23:56:00+00:00")
        assert (loss["outcome"], loss["r"]) == ("loss", -1.0)
        assert win["reason"] == "a, b"
        assert win["atr"] is None and win["config_hash"] is None

    def test_current_columns_survive_and_open_trades_have_no_r(self, conn, tmp_path):
        path = tmp_path / "trades.csv"
        path.write_text(CURRENT_CSV)
        alert_log.import_trades_csv(conn, path, "backtest:x", "XAU_USD", "1m")
        loss, still_open = rows(conn, "backtest:x")
        assert (loss["bars_held"], loss["atr"], loss["stack_bars"], loss["config_hash"]) == \
            (7, 2.0, 25, "abc123")
        assert loss["bars_since_last_alert"] is None
        assert (still_open["outcome"], still_open["r"], still_open["exit_time"]) == \
            ("open", None, None)

    def test_backtest_rows_never_mix_with_live(self, conn, tmp_path):
        path = tmp_path / "trades.csv"
        path.write_text(LEGACY_CSV)
        alert_log.import_trades_csv(conn, path, "backtest:legacy", "T", "1m")
        assert rows(conn, "live") == []

    def test_live_and_backtest_can_hold_the_same_bar(self, conn, tmp_path):
        rec = dict(ALERT, bar_time="2026-08-02 22:28:00+00:00")
        alert_log.ingest_jsonl(conn, jsonl(tmp_path / "a.jsonl", rec), "XAU_USD", "1m")
        path = tmp_path / "trades.csv"
        path.write_text(LEGACY_CSV)
        alert_log.import_trades_csv(conn, path, "backtest:legacy", "XAU_USD", "1m")
        ids = {r["alert_id"] for r in rows(conn)} & {r["alert_id"] for r in rows(conn, "backtest:legacy")}
        assert ids == {"XAU_USD|BUY|2026-08-02T22:28:00+00:00"}

    def test_latest_backtest_source_is_the_last_imported(self, conn):
        trade = {"entry_time": "2026-09-01 10:00", "signal": "BUY", "outcome": "win", "r": 1.5}
        alert_log.import_trades(conn, [trade], "backtest:2026-10-01T00:00:00+00:00", "T", "1m")
        alert_log.import_trades(conn, [trade], "backtest:legacy", "T", "1m")
        assert alert_log.latest_backtest_source(conn) == "backtest:legacy"


class TestReadOnly:
    def test_missing_file_is_not_created(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            alert_log.connect(tmp_path / "none.db", readonly=True)
        assert not (tmp_path / "none.db").exists()

    def test_read_only_connection_refuses_writes(self, tmp_path):
        alert_log.connect(tmp_path / "a.db").close()
        ro = alert_log.connect(tmp_path / "a.db", readonly=True)
        with pytest.raises(alert_log.sqlite3.OperationalError):
            ro.execute("DELETE FROM alerts")
        ro.close()


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

def trade(bar_time, signal="BUY", outcome="win", exit_time=None, r=None, **fields):
    if r is None:
        r = 1.5 if outcome == "win" else (-1.0 if outcome == "loss" else None)
    row = {"bar_time": bar_time, "signal": signal, "outcome": outcome, "exit_time": exit_time,
           "r": r, "depth": 1, "tier": "STRONG", "strength": 0.7, "stack_bars": 20,
           "bars_since_last_alert": None}
    row.update(fields)
    return row


def t(hour, minute=0):
    return f"2026-10-05T{hour:02d}:{minute:02d}:00+00:00"


class TestAnnotate:
    def test_session_is_cut_on_utc_hour(self):
        sessions = [a["session"] for a in alert_log.annotate(
            [trade(t(h), exit_time=t(h, 30)) for h in (0, 7, 12, 17, 22)]
        )]
        assert sessions == ["Asia", "London", "NY", "Late", "Asia"]

    def test_prev_same_dir_is_what_was_known_at_the_bar(self):
        a = alert_log.annotate([
            trade(t(1), outcome="loss", exit_time=t(1, 10)),
            trade(t(1, 5), signal="SELL", outcome="win", exit_time=t(3)),
            trade(t(2)),                                   # previous BUY lost at 01:10
            trade(t(2, 30), signal="SELL", exit_time=t(4)),  # previous SELL still open
        ])
        assert [x["prev_same_dir"] for x in a] == ["none", "none", "loss", "open"]

    def test_expired_previous_is_unknown(self):
        a = alert_log.annotate([trade(t(1), outcome="expired"), trade(t(2))])
        assert a[1]["prev_same_dir"] == "unknown"

    def test_overlaps_open_needs_an_earlier_alert_not_yet_exited(self):
        a = alert_log.annotate([
            trade(t(1), outcome="loss", exit_time=t(1, 30)),
            trade(t(2), outcome="win", exit_time=t(5)),
            trade(t(3), signal="SELL", outcome="loss", exit_time=t(3, 10)),  # inside 02:00's trade
            trade(t(6)),                                                    # everything closed
            trade(t(7), outcome="pending"),
            trade(t(8)),                                                    # 07:00 unresolved
        ])
        assert [x["overlaps_open"] for x in a] == ["no", "no", "yes", "no", "no", "yes"]


class TestBucketStats:
    def test_hand_computed_mean_and_standard_error(self):
        # 2 wins at 1.5R and 3 losses: mean 0.0, sample sd sqrt(1.875) -> se 0.6124
        rs = [1.5, 1.5, -1.0, -1.0, -1.0]
        data = alert_log.annotate([trade(t(1, i), r=r, outcome="win" if r > 0 else "loss",
                                         exit_time=t(1, i)) for i, r in enumerate(rs)])
        (b,) = alert_log.bucket_stats(data, ["signal"], min_n=1)["buckets"]
        assert (b["bucket_id"], b["n"], b["wins"], b["win_rate"]) == ("signal=BUY", 5, 2, 0.4)
        assert b["mean_r"] == 0.0
        assert b["se_r"] == pytest.approx(math.sqrt(1.875) / math.sqrt(5), abs=1e-4)
        assert b["win_rate_se"] == pytest.approx(math.sqrt(0.24 / 5), abs=1e-4)

    def test_only_closed_trades_count(self):
        data = alert_log.annotate([trade(t(1), exit_time=t(1, 5)), trade(t(2), outcome="open"),
                                   trade(t(3), outcome="expired")])
        assert alert_log.bucket_stats(data, ["signal"])["n"] == 1

    def test_small_buckets_are_noise_and_never_significant(self):
        data = alert_log.annotate([trade(t(1, i), outcome="loss", exit_time=t(1, i))
                                   for i in range(10)])
        (b,) = alert_log.bucket_stats(data, ["signal"], min_n=30)["buckets"]
        assert b["noise"] is True
        assert b["sig_negative"] is False

    def test_a_clearly_losing_bucket_is_significant(self):
        data = alert_log.annotate(
            [trade(t(i // 60, i % 60), outcome="loss", exit_time=t(i // 60, i % 60))
             for i in range(36)]
            + [trade(t(i // 60, i % 60), outcome="win", exit_time=t(i // 60, i % 60))
               for i in range(36, 40)]
        )
        (b,) = alert_log.bucket_stats(data, ["signal"], min_n=30)["buckets"]
        assert b["sig_negative"] is True

    def test_more_buckets_raise_the_bar(self):
        # 10 wins in 40: mean -0.375R, se ~0.17. More than 2 SE below zero, but
        # not once the critical value is corrected across many buckets.
        rows_ = ([trade(t(i // 60, i % 60), outcome="loss", exit_time=t(0)) for i in range(30)]
                 + [trade(t(i // 60, i % 60), outcome="win", exit_time=t(0)) for i in range(30, 40)])
        data = alert_log.annotate(rows_)
        one = alert_log.bucket_stats(data, ["signal"], min_n=30)
        many = alert_log.bucket_stats(data, list(alert_log.DIMENSIONS), min_n=30)
        assert one["k"] == 1 and many["k"] > 5
        assert many["z"] > one["z"]
        assert one["buckets"][0]["sig_negative"] is True
        sig = {b["bucket_id"]: b["sig_negative"] for b in many["buckets"]}
        assert sig["signal=BUY"] is False

    def test_breakeven_follows_rr(self):
        assert alert_log.bucket_stats([], ["signal"], rr=1.5)["breakeven_win_rate"] == 0.4
        assert alert_log.bucket_stats([], ["signal"], rr=2.0)["breakeven_win_rate"] == 0.3333

    def test_buckets_come_out_in_display_order(self):
        data = alert_log.annotate([trade(t(h), exit_time=t(h, 1)) for h in (17, 12, 7, 0)])
        stats = alert_log.bucket_stats(data, ["session"], min_n=1)
        assert [b["bucket"] for b in stats["buckets"]] == ["Asia", "London", "NY", "Late"]

    def test_every_dimension_runs_on_sparse_rows(self):
        data = alert_log.annotate([trade(t(1), exit_time=t(1, 5), depth=None, tier=None,
                                         strength=None, stack_bars=None)])
        stats = alert_log.bucket_stats(data, list(alert_log.DIMENSIONS), min_n=1)
        assert len(stats["buckets"]) == len(alert_log.DIMENSIONS)
