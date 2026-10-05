"""
Tests for dashboard/. Flask test client only: no network, no real
systemctl or journalctl, and the bot's files live in tmp_path.
"""
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import signal_bot
from dashboard import actions
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


# ---------------------------------------------------------------------------
# Actions: auth, routing, locking
# ---------------------------------------------------------------------------

TOKEN = {"X-Token": "test-token"}


@pytest.fixture(autouse=True)
def clean_action_state(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    app_module._attempts.clear()
    yield
    app_module._attempts.clear()


@pytest.fixture
def executed(monkeypatch):
    """Record actions that actually ran, without running them."""
    ran = []

    def fake_execute(action):
        ran.append(action.name)
        return {"ok": True, "output": "", "duration_ms": 0}

    monkeypatch.setattr(app_module.actions, "execute", fake_execute)
    return ran


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Capture subprocess.run calls made by actions.py."""
    calls = []
    result = {"returncode": 0, "stdout": "done\n", "stderr": ""}

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, result["returncode"],
                                           result["stdout"], result["stderr"])

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    return calls, result


ALL_ACTIONS = sorted(actions.ACTIONS)


class TestActionAuth:
    @pytest.mark.parametrize("name", ALL_ACTIONS + ["not_an_action"])
    def test_missing_token_is_401(self, client, executed, name):
        resp = client.post(f"/api/action/{name}")
        assert resp.status_code == 401
        assert resp.json["ok"] is False
        assert executed == []

    @pytest.mark.parametrize("name", ALL_ACTIONS)
    def test_wrong_token_is_401(self, client, executed, name):
        resp = client.post(f"/api/action/{name}", headers={"X-Token": "test-tokeN"})
        assert resp.status_code == 401
        assert executed == []

    def test_unset_token_refuses_every_action(self, client, executed, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "")
        resp = client.post("/api/action/test_alert", headers={"X-Token": ""})
        assert resp.status_code == 503
        assert "DASHBOARD_TOKEN" in resp.json["output"]
        assert executed == []

    def test_compare_is_constant_time(self, client, executed, monkeypatch):
        seen = []
        real = app_module.hmac.compare_digest
        monkeypatch.setattr(
            app_module.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b)
        )
        client.post("/api/action/poll_now", headers=TOKEN)
        assert seen == [(b"test-token", b"test-token")]

    def test_valid_token_runs_the_action(self, client, executed):
        resp = client.post("/api/action/poll_now", headers=TOKEN)
        assert resp.status_code == 200
        assert executed == ["poll_now"]

    def test_get_is_not_allowed(self, client, executed):
        assert client.get("/api/action/poll_now", headers=TOKEN).status_code == 405
        assert executed == []

    def test_token_is_never_echoed(self, client, executed):
        body = client.get("/").get_data(as_text=True)
        body += client.get("/api/config").get_data(as_text=True)
        assert "test-token" not in body


class TestActionRouting:
    def test_unknown_action_is_404(self, client, executed):
        resp = client.post("/api/action/rm_rf", headers=TOKEN)
        assert resp.status_code == 404
        assert executed == []

    def test_busy_is_409(self, client):
        assert actions._lock.acquire(blocking=False)
        try:
            resp = client.post("/api/action/clear_state", headers=TOKEN)
        finally:
            actions._lock.release()
        assert resp.status_code == 409
        assert resp.json["ok"] is False

    def test_lock_is_released_after_an_action(self, client, fake_subprocess):
        client.post("/api/action/poll_now", headers=TOKEN)
        assert client.post("/api/action/poll_now", headers=TOKEN).status_code == 200

    def test_lock_is_released_after_an_action_raises(self, client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("kaput")

        monkeypatch.setattr(actions.subprocess, "run", boom)
        resp = client.post("/api/action/poll_now", headers=TOKEN)
        assert resp.json == {"ok": False, "output": "RuntimeError: kaput",
                             "duration_ms": resp.json["duration_ms"]}
        assert not actions._lock.locked()

    def test_result_shape(self, client, fake_subprocess):
        resp = client.post("/api/action/poll_now", headers=TOKEN)
        assert set(resp.json) == {"ok", "output", "duration_ms"}
        assert resp.json["ok"] is True
        assert resp.json["output"] == "done"

    def test_actions_are_logged_with_client_ip(self, client, fake_subprocess, caplog):
        caplog.set_level(logging.INFO, logger="dashboard")
        client.post("/api/action/poll_now", headers=TOKEN,
                    environ_base={"REMOTE_ADDR": "192.168.1.23"})
        client.post("/api/action/poll_now", environ_base={"REMOTE_ADDR": "192.168.1.66"})
        assert "poll_now from 192.168.1.23 ok=True" in caplog.text
        assert "poll_now from 192.168.1.66 refused (401)" in caplog.text


class TestRateLimitAndOrigin:
    def test_eleventh_attempt_in_a_minute_is_429(self, client, executed):
        codes = [client.post("/api/action/poll_now", headers=TOKEN).status_code
                 for _ in range(app_module.RATE_LIMIT + 1)]
        assert codes == [200] * app_module.RATE_LIMIT + [429]

    def test_failed_attempts_count_too(self, client, executed):
        for _ in range(app_module.RATE_LIMIT):
            client.post("/api/action/poll_now", headers={"X-Token": "guess"})
        assert client.post("/api/action/poll_now", headers=TOKEN).status_code == 429
        assert executed == []

    def test_limit_is_per_ip(self, client, executed):
        for _ in range(app_module.RATE_LIMIT):
            client.post("/api/action/poll_now", headers=TOKEN,
                        environ_base={"REMOTE_ADDR": "10.0.0.1"})
        resp = client.post("/api/action/poll_now", headers=TOKEN,
                           environ_base={"REMOTE_ADDR": "10.0.0.2"})
        assert resp.status_code == 200

    def test_window_slides(self):
        for i in range(app_module.RATE_LIMIT):
            assert not app_module.rate_limited("1.2.3.4", now=100.0 + i)
        assert app_module.rate_limited("1.2.3.4", now=110.0)
        assert not app_module.rate_limited("1.2.3.4", now=160.5)

    def test_cross_origin_post_is_403(self, client, executed):
        resp = client.post("/api/action/poll_now",
                           headers={**TOKEN, "Origin": "http://evil.example"})
        assert resp.status_code == 403
        assert executed == []

    def test_null_origin_is_403(self, client, executed):
        resp = client.post("/api/action/poll_now", headers={**TOKEN, "Origin": "null"})
        assert resp.status_code == 403

    def test_same_origin_post_is_allowed(self, client, executed):
        resp = client.post("/api/action/poll_now",
                           headers={**TOKEN, "Origin": "http://localhost"})
        assert resp.status_code == 200


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", ["/", "/api/status", "/static/app.js", "/healthz"])
    def test_headers_on_every_response(self, client, path):
        resp = client.get(path)
        assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_no_cors_on_actions(self, client, executed):
        resp = client.post("/api/action/poll_now",
                           headers={**TOKEN, "Origin": "http://localhost"})
        assert not any(h.lower().startswith("access-control-") for h in resp.headers.keys())

    def test_api_responses_are_not_cached(self, client):
        assert client.get("/api/status").headers["Cache-Control"] == "no-store"

    def test_page_has_no_inline_script_or_style(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "<script>" not in html and "<style" not in html and " style=" not in html

    def test_buttons_disabled_without_a_token(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "")
        html = client.get("/").get_data(as_text=True)
        assert html.count("DASHBOARD_TOKEN is not set") == len(actions.ACTIONS)


# ---------------------------------------------------------------------------
# Actions: what they run
# ---------------------------------------------------------------------------

class TestSubprocessSafety:
    @pytest.mark.parametrize("name", ["poll_now", "update_deps",
                                      "start_bot", "restart_bot", "stop_bot"])
    def test_list_args_no_shell_and_a_timeout(self, client, fake_subprocess, monkeypatch, name):
        monkeypatch.setattr(actions.sys, "platform", "linux")
        calls, _ = fake_subprocess
        client.post(f"/api/action/{name}", headers=TOKEN)
        (args, kwargs), = calls
        assert isinstance(args, list) and all(isinstance(a, str) for a in args)
        assert kwargs.get("shell") is not True
        assert kwargs["timeout"] > 0
        assert kwargs["stdin"] is subprocess.DEVNULL

    @pytest.mark.parametrize("name,verb", [
        ("start_bot", "start"), ("restart_bot", "restart"), ("stop_bot", "stop"),
    ])
    def test_systemctl_matches_the_sudoers_rule(self, client, fake_subprocess, monkeypatch,
                                                name, verb):
        monkeypatch.setattr(actions.sys, "platform", "linux")
        calls, _ = fake_subprocess
        client.post(f"/api/action/{name}", headers=TOKEN)
        (args, _kwargs), = calls
        assert args == ["sudo", "-n", "/bin/systemctl", verb, "signal-bot"]
        rule = (Path(__file__).parent / "deploy" / "sudoers-signal-bot").read_text()
        assert f"/bin/systemctl {verb} signal-bot" in rule

    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    @pytest.mark.parametrize("name", ["start_bot", "restart_bot", "stop_bot"])
    def test_non_linux_is_unsupported_without_calling_systemctl(
        self, client, fake_subprocess, monkeypatch, platform, name
    ):
        monkeypatch.setattr(actions.sys, "platform", platform)
        calls, _ = fake_subprocess
        resp = client.post(f"/api/action/{name}", headers=TOKEN)
        assert resp.status_code == 200
        assert resp.json["ok"] is False
        assert "not supported" in resp.json["output"]
        assert calls == []

    def test_failed_command_is_not_ok(self, client, fake_subprocess):
        _, result = fake_subprocess
        result.update(returncode=1, stdout="", stderr="Traceback: boom")
        resp = client.post("/api/action/poll_now", headers=TOKEN)
        assert resp.json["ok"] is False
        assert "boom" in resp.json["output"]

    def test_timeout_is_reported(self, client, monkeypatch):
        def slow(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"partial")

        monkeypatch.setattr(actions.subprocess, "run", slow)
        resp = client.post("/api/action/poll_now", headers=TOKEN)
        assert resp.json["ok"] is False
        assert "timed out" in resp.json["output"]
        assert "partial" in resp.json["output"]

    def test_output_is_truncated_to_4kb_keeping_the_tail(self, client, fake_subprocess):
        _, result = fake_subprocess
        result["stdout"] = "x" * 10_000 + "THE END"
        out = client.post("/api/action/poll_now", headers=TOKEN).json["output"]
        assert len(out) <= actions.OUTPUT_LIMIT + 20
        assert out.endswith("THE END")

    def test_output_is_redacted(self, client, fake_subprocess):
        _, result = fake_subprocess
        result["stderr"] = f"HTTPError: 404 Client Error for url: {SENTINEL}"
        body = client.post("/api/action/poll_now", headers=TOKEN).get_data(as_text=True)
        assert SENTINEL_TAIL not in body


class TestActionBehaviour:
    def test_test_alert_posts_a_labelled_message(self, client, monkeypatch):
        posted = []

        class Resp:
            status_code = 204

            def raise_for_status(self):
                pass

        monkeypatch.setattr(actions.requests, "post",
                            lambda url, json, timeout: posted.append((url, json)) or Resp())
        resp = client.post("/api/action/test_alert", headers=TOKEN)
        assert resp.json["ok"] is True
        (url, payload), = posted
        assert url == SENTINEL
        assert "Test message" in payload["content"]
        assert SENTINEL_TAIL not in resp.get_data(as_text=True)

    def test_test_alert_failure_does_not_leak_the_webhook(self, client, monkeypatch):
        def fail(url, json, timeout):
            raise actions.requests.HTTPError(f"404 Client Error: Not Found for url: {url}")

        monkeypatch.setattr(actions.requests, "post", fail)
        resp = client.post("/api/action/test_alert", headers=TOKEN)
        assert resp.json["ok"] is False
        assert SENTINEL_TAIL not in resp.get_data(as_text=True)

    def test_clear_state_deletes_the_state_file(self, client):
        status.state_path().write_text('{"signal": "BUY"}')
        resp = client.post("/api/action/clear_state", headers=TOKEN)
        assert resp.json["ok"] is True
        assert not status.state_path().exists()

    def test_clear_state_without_a_file_is_fine(self, client):
        resp = client.post("/api/action/clear_state", headers=TOKEN)
        assert resp.json["ok"] is True
        assert "did not exist" in resp.json["output"]

    def test_poll_now_runs_run_once_in_the_bot_directory(self, client, fake_subprocess):
        calls, _ = fake_subprocess
        client.post("/api/action/poll_now", headers=TOKEN)
        (args, kwargs), = calls
        assert args[0] == sys.executable
        assert "signal_bot.run_once()" in args[-1]
        assert kwargs["cwd"] == status.ROOT

    def test_update_deps_uses_this_interpreters_pip(self, client, fake_subprocess):
        calls, _ = fake_subprocess
        client.post("/api/action/update_deps", headers=TOKEN)
        (args, _kwargs), = calls
        assert args == [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]

    def test_destructive_actions_require_confirmation(self):
        confirmed = {name for name, a in actions.ACTIONS.items() if a.confirm}
        assert confirmed == {"restart_bot", "stop_bot", "clear_state", "update_deps"}


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "ok"


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"app.js" in resp.data
