"""
Tests for dashboard/. Flask test client only: no network, no real
systemctl or journalctl, and the bot's files live in tmp_path.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import signal_bot
from dashboard import app as app_module
from dashboard import status

SENTINEL = "https://discord.com/api/webhooks/999/SENTINEL-do-not-leak"
SENTINEL_TAIL = "SENTINEL-do-not-leak"
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def bot_files(tmp_path, monkeypatch):
    """Point the dashboard at tmp_path and make the webhook a sentinel."""
    monkeypatch.setattr(status, "ROOT", tmp_path)
    monkeypatch.setenv("SIGNAL_TICKER", "TESTTICKER")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SENTINEL)
    monkeypatch.setattr(signal_bot, "DISCORD_WEBHOOK_URL", SENTINEL)
    monkeypatch.delenv("DASHBOARD_LOG_FILE", raising=False)
    return tmp_path


@pytest.fixture
def client():
    return app_module.app.test_client()


def write_status(age_seconds=0, **fields):
    payload = {
        "ts_utc": (NOW - timedelta(seconds=age_seconds)).isoformat(timespec="seconds"),
        "ticker": "TESTTICKER", "interval": "1m", "poll_seconds": 30,
        "result": "no_setup", "last_poll_ok": True, "last_error": None,
        "consecutive_errors": 0, "close": 4650.5, "stack": "bull",
    }
    payload.update(fields)
    status.status_path().write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.parametrize("age,expected", [
        (0, "ok"), (60, "ok"), (61, "stale"), (300, "stale"), (301, "down"),
    ])
    def test_thresholds_are_two_and_ten_polls(self, age, expected):
        assert status.health(age, 30, True) == expected

    def test_fresh_but_failing_is_error(self):
        assert status.health(5, 30, False) == "error"

    def test_stale_outranks_error(self):
        assert status.health(100, 30, False) == "stale"

    def test_no_age_is_no_data(self):
        assert status.health(None, 30, True) == "no_data"

    def test_thresholds_follow_the_bots_poll_interval(self):
        write_status(age_seconds=100, poll_seconds=60)
        assert status.load_status(now=NOW)["health"] == "ok"

    def test_load_status_computes_age_and_health(self):
        write_status(age_seconds=90)
        data = status.load_status(now=NOW)
        assert data["age_seconds"] == 90
        assert data["health"] == "stale"
        assert data["status"]["close"] == 4650.5

    def test_clock_skew_does_not_give_negative_age(self):
        write_status(age_seconds=-30)
        assert status.load_status(now=NOW)["age_seconds"] == 0


# ---------------------------------------------------------------------------
# Missing and corrupt files
# ---------------------------------------------------------------------------

class TestBadFiles:
    def test_missing_status_file_is_no_data_not_500(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json["health"] == "no_data"
        assert "no data yet" in resp.json["message"]
        assert resp.json["status"] is None

    def test_corrupt_status_file(self, client):
        status.status_path().write_text("{not json")
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json["health"] == "no_data"
        assert "unreadable" in resp.json["message"]

    def test_status_file_that_is_not_an_object(self, client):
        status.status_path().write_text("[1, 2]")
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json["health"] == "no_data"

    def test_unparseable_timestamp(self, client):
        write_status(ts_utc="yesterday")
        assert client.get("/api/status").json["health"] == "no_data"

    def test_missing_state_file(self, client):
        resp = client.get("/api/last-signal")
        assert resp.status_code == 200
        assert resp.json["last_signal"] is None
        assert resp.json["message"] == "no alert sent yet"

    def test_corrupt_state_file(self, client):
        status.state_path().write_text("{oops")
        resp = client.get("/api/last-signal")
        assert resp.status_code == 200
        assert resp.json["last_signal"] is None
        assert "unreadable" in resp.json["message"]

    def test_state_file_without_a_signal(self, client):
        status.state_path().write_text("{}")
        resp = client.get("/api/last-signal")
        assert resp.status_code == 200
        assert resp.json["last_signal"] is None

    def test_legacy_bare_string_state(self, client):
        status.state_path().write_text('"SELL"')
        assert client.get("/api/last-signal").json["last_signal"]["signal"] == "SELL"


class TestLastSignal:
    def test_reads_what_the_bot_writes(self, client, monkeypatch, bot_files):
        monkeypatch.setattr(signal_bot, "STATE_FILE", status.state_path())
        signal_bot.save_last_signal("BUY", "STRONG", 2, "2026-10-05 11:59:00+00:00")
        assert client.get("/api/last-signal").json["last_signal"] == {
            "signal": "BUY", "tier": "STRONG", "depth": 2,
            "bar_time": "2026-10-05 11:59:00+00:00",
        }

    def test_status_written_by_the_bot_round_trips(self, client, monkeypatch):
        monkeypatch.setattr(signal_bot, "STATUS_FILE", status.status_path())
        signal_bot.write_status(result="sent", last_poll_ok=True, close=1.0)
        data = client.get("/api/status").json
        assert data["health"] == "ok"
        assert data["status"]["result"] == "sent"

    def test_unknown_status_fields_are_dropped(self, client):
        write_status(surprise="hello")
        assert "surprise" not in client.get("/api/status").json["status"]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

class TestSecrets:
    def test_config_excludes_the_webhook_by_construction(self):
        # Before any response-level redaction runs.
        assert SENTINEL_TAIL not in json.dumps(status.bot_config())

    def test_config_response_has_no_webhook(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json["config"]["ticker"] == signal_bot.TICKER
        assert "webhook" not in resp.get_data(as_text=True).lower()

    def test_webhook_in_last_error_is_redacted(self, client):
        write_status(last_error=f"HTTPError: 404 for url: {SENTINEL}")
        body = client.get("/api/status").get_data(as_text=True)
        assert SENTINEL_TAIL not in body
        assert "redacted" in body

    def test_any_discord_webhook_shape_is_redacted(self):
        other = "https://canary.discordapp.com/api/webhooks/1/abcdef"
        assert "abcdef" not in status.redact(f"failed: {other}")

    def test_secret_env_values_are_redacted(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_TOKEN", "oanda-secret-123")
        assert "oanda-secret-123" not in status.redact("Bearer oanda-secret-123")

    def test_webhook_in_logs_is_redacted(self, client, monkeypatch, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text(f"[X] error this cycle: 404 for url: {SENTINEL}\n")
        monkeypatch.setenv("DASHBOARD_LOG_FILE", str(log))
        assert SENTINEL_TAIL not in client.get("/api/logs").get_data(as_text=True)

    @pytest.mark.parametrize("path", [
        "/", "/api/status", "/api/last-signal", "/api/config", "/api/logs", "/healthz",
    ])
    def test_webhook_appears_in_no_get_response(self, client, path, monkeypatch, tmp_path):
        write_status(last_error=SENTINEL)
        status.state_path().write_text(json.dumps({"signal": "BUY", "tier": SENTINEL}))
        log = tmp_path / "bot.log"
        log.write_text(SENTINEL + "\n")
        monkeypatch.setenv("DASHBOARD_LOG_FILE", str(log))
        assert SENTINEL_TAIL not in client.get(path).get_data(as_text=True)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class TestLogs:
    def test_tails_the_log_file(self, client, monkeypatch, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text("".join(f"line {i}\n" for i in range(50)))
        monkeypatch.setenv("DASHBOARD_LOG_FILE", str(log))
        data = client.get("/api/logs?n=3").json
        assert data["lines"] == ["line 47", "line 48", "line 49"]
        assert data["source"] == "file bot.log"

    def test_n_is_clamped(self, client, monkeypatch, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text("".join(f"line {i}\n" for i in range(1000)))
        monkeypatch.setenv("DASHBOARD_LOG_FILE", str(log))
        assert len(client.get("/api/logs?n=100000").json["lines"]) == app_module.MAX_LOG_LINES
        assert len(client.get("/api/logs?n=-5").json["lines"]) == 1

    def test_missing_log_file_is_a_message_not_a_500(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("DASHBOARD_LOG_FILE", str(tmp_path / "nope.log"))
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        assert "cannot read log" in resp.json["message"]

    def test_uses_journalctl_on_linux(self, client, monkeypatch):
        calls = []

        class Proc:
            returncode = 0
            stdout = "a\nb\n"
            stderr = ""

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return Proc()

        monkeypatch.setattr(status.sys, "platform", "linux")
        monkeypatch.setattr(status.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(status.subprocess, "run", fake_run)
        data = client.get("/api/logs?n=20").json
        assert data["lines"] == ["a", "b"]
        (args, kwargs), = calls
        assert args[:3] == ["journalctl", "-u", "signal-bot"]
        assert "20" in args
        assert kwargs.get("shell") is not True
        assert kwargs["timeout"]

    def test_no_log_source_elsewhere(self, client, monkeypatch):
        monkeypatch.setattr(status.sys, "platform", "win32")
        data = client.get("/api/logs").json
        assert data["lines"] == []
        assert "DASHBOARD_LOG_FILE" in data["message"]


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "ok"


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"app.js" in resp.data
