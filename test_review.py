"""
Tests for review.py: the sample-size gate, the guardrails on proposals, the
report, and that every failure ends in a report rather than a crash.

Run locally:
    pytest test_review.py -v

No network and no Anthropic SDK needed: the client is a fake object.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import alert_log
import review
import signal_bot

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def alert(i, depth, win, config=None, **fields):
    t = START + timedelta(minutes=30 * i)
    row = {c: None for c in alert_log.COLUMNS}
    row.update(
        source="live", alert_id=f"T|BUY|{t.isoformat()}", ticker="T", interval="1m",
        config_hash=config or signal_bot.config_hash(), bar_time=t.isoformat(),
        signal="BUY", tier="STRONG", depth=depth, strength=0.7, stack_bars=20,
        outcome="win" if win else "loss", r=1.5 if win else -1.0,
        exit_time=(t + timedelta(minutes=5)).isoformat(),
    )
    row.update(fields)
    return row


def standard_rows():
    """150 closed alerts: depth 1 wins half, depth 2 wins 1 in 10 (clearly losing)."""
    rows = [alert(i, 1, i % 2 == 0) for i in range(90)]
    rows += [alert(90 + i, 2, i % 10 == 0) for i in range(60)]
    return rows


def make_db(path, rows):
    conn = alert_log.connect(path)
    alert_log.insert(conn, rows)
    conn.commit()
    conn.close()
    return path


class FakeClient:
    """Stands in for anthropic.Anthropic(): records requests, returns a canned answer."""

    def __init__(self, answer=None, stop_reason="end_turn", raises=None):
        self.calls = []
        client = self

        class Messages:
            def create(self, **kwargs):
                client.calls.append(kwargs)
                if raises is not None:
                    raise raises
                text = answer if isinstance(answer, str) else json.dumps(answer)
                return SimpleNamespace(
                    stop_reason=stop_reason,
                    content=[SimpleNamespace(type="thinking", thinking=""),
                             SimpleNamespace(type="text", text=text)],
                    usage=SimpleNamespace(input_tokens=20000, output_tokens=3000),
                )

        self.beta = SimpleNamespace(messages=Messages())


class ExplodingClient:
    """Fails the test if anything tries to call the model."""

    @property
    def beta(self):
        raise AssertionError("the model must not be called")


def answer(**fields):
    base = {"verdict": "no_action", "summary": "Nothing stands out.", "loss_clusters": [],
            "negative_buckets": [], "proposals": [], "hypotheses": [], "caveats": []}
    base.update(fields)
    return base


SHORTS_PROPOSAL = {
    "env_key": "SIGNAL_SHORT_MAX_DEPTH", "proposed_value": "1", "bucket_ids": ["depth=2"],
    "rationale": "Depth 2 loses.", "backtest_command": "python backtest.py --days 60",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(signal_bot, "SHORT_MAX_DEPTH", 2)

    class Env:
        db = tmp_path / ".alerts_T.db"
        reports = tmp_path / "reports"

        def run(self, client=None, **kw):
            return review.run(client=client, now=NOW, db=self.db,
                              reports_dir=self.reports, post=kw.pop("post", False), **kw)

        def report(self):
            return (self.reports / "2026-10-06.md").read_text(encoding="utf-8")

    return Env()


# ---------------------------------------------------------------------------
# The sample-size gate
# ---------------------------------------------------------------------------

class TestGate:
    def test_no_alert_log_is_exit_2(self, env):
        assert env.run(ExplodingClient()) == 2

    def test_too_few_alerts_never_calls_the_model(self, env):
        make_db(env.db, [alert(i, 1, True) for i in range(10)])
        assert env.run(ExplodingClient()) == 0
        text = env.report()
        assert "## Insufficient data" in text
        assert "No model was asked" in text
        assert "**Verdict:** insufficient data" in text

    def test_alerts_under_another_config_do_not_count(self, env):
        make_db(env.db, [alert(i, 1, True, config="old") for i in range(150)])
        env.run(ExplodingClient())
        assert "**Closed live alerts:** 0" in env.report()

    def test_alerts_outside_the_window_do_not_count(self, env):
        old = [alert(i, 1, True, bar_time="2026-08-01T00:00:00+00:00",
                     exit_time="2026-08-01T00:05:00+00:00", alert_id=f"old{i}")
               for i in range(150)]
        make_db(env.db, old)
        env.run(ExplodingClient())
        assert "insufficient data" in env.report()

    def test_no_llm_writes_the_stats_without_a_client(self, env):
        make_db(env.db, standard_rows())
        assert env.run(ExplodingClient(), no_llm=True) == 0
        text = env.report()
        assert "**Verdict:** stats only" in text
        assert "| `depth=2` | 60 |" in text
        assert "**losing**" in text

    def test_dry_run_prints_the_prompt_and_writes_nothing(self, env, capsys):
        make_db(env.db, standard_rows())
        assert env.run(ExplodingClient(), dry_run=True) == 0
        assert "Breakeven win rate" in capsys.readouterr().out
        assert not env.reports.exists()


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

class TestRequest:
    def test_prompt_carries_base_rate_multiple_comparisons_and_data(self, env):
        make_db(env.db, standard_rows())
        client = FakeClient(answer())
        env.run(client)
        (call,) = client.calls
        assert call["model"] == review.MODEL
        assert call["output_config"]["format"] == {"type": "json_schema", "schema": review.SCHEMA}
        system, user = call["system"], call["messages"][0]["content"]
        assert "Breakeven win rate = 1/(1+RR) = 40%" in system
        assert "would look significant by" in system
        assert "Never guess" in system
        assert "depth=2" in user
        assert "Hard constraints" in user          # docs/LEARNINGS.md went in
        assert "alert_id,signal,depth" in user     # the alert table
        assert len(user.splitlines()) > 150

    def test_schema_is_closed_at_every_level(self):
        def walk(node):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
                for child in node["properties"].values():
                    walk(child)
            if node.get("type") == "array":
                walk(node["items"])
        walk(review.SCHEMA)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

class TestGuardrails:
    @pytest.fixture
    def stats(self):
        return alert_log.bucket_stats(alert_log.annotate(standard_rows()),
                                      list(alert_log.DIMENSIONS), min_n=30)

    @pytest.fixture
    def config(self):
        return dict(review.current_config(), SIGNAL_SHORT_MAX_DEPTH=2, SIGNAL_REQUIRE_PULLBACK=True)

    def test_a_proposal_resting_on_a_losing_bucket_survives(self, stats, config):
        accepted, rejected = review.check_proposals([SHORTS_PROPOSAL], stats, config)
        assert rejected == []
        assert accepted[0]["value"] == 1
        assert accepted[0]["buckets"][0]["bucket_id"] == "depth=2"

    @pytest.mark.parametrize("change,reason", [
        ({"bucket_ids": ["depth=1"]}, "not significantly negative"),
        ({"bucket_ids": ["depth=9"]}, "unknown buckets"),
        ({"bucket_ids": []}, "cites no bucket"),
        ({"env_key": "SIGNAL_FRACTAL_N"}, "not a setting a review may change"),
        ({"env_key": "SIGNAL_SESSION_GAP_MULT"}, "not a setting a review may change"),
        ({"env_key": "SIGNAL_DROP_UNCLOSED_BAR"}, "not a setting a review may change"),
        ({"env_key": "DISCORD_WEBHOOK_URL"}, "not a setting a review may change"),
        ({"proposed_value": "two"}, "does not parse"),
        ({"proposed_value": "2"}, "current value"),
        ({"env_key": "SIGNAL_REQUIRE_PULLBACK", "proposed_value": "maybe"}, "does not parse"),
    ])
    def test_anything_else_is_rejected_with_a_reason(self, stats, config, change, reason):
        accepted, (rejected,) = review.check_proposals([dict(SHORTS_PROPOSAL, **change)],
                                                       stats, config)
        assert accepted == []
        assert reason in rejected["reason"]

    def test_noise_buckets_can_never_be_significant(self, config):
        small = alert_log.bucket_stats(alert_log.annotate(standard_rows()[:100]),
                                       ["depth"], min_n=30)
        (d2,) = [b for b in small["buckets"] if b["bucket_id"] == "depth=2"]
        assert d2["noise"] is True
        _, rejected = review.check_proposals([SHORTS_PROPOSAL], small, config)
        assert rejected

    def test_every_allowed_key_is_a_documented_setting(self):
        example = (Path(review.__file__).parent / ".env.example").read_text()
        for key, attr in review.ALLOWED_KEYS.items():
            assert f"\n{key}=" in example
            assert hasattr(signal_bot, attr)

    def test_hard_constraints_are_not_allowed(self):
        for key in ("SIGNAL_FRACTAL_N", "SIGNAL_SESSION_GAP_MULT", "SIGNAL_DROP_UNCLOSED_BAR",
                    "SIGNAL_INTERVAL", "SIGNAL_TICKER", "SIGNAL_POLL_SECONDS"):
            assert key not in review.ALLOWED_KEYS


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

class TestReport:
    def test_accepted_proposal_becomes_a_diff_with_code_computed_numbers(self, env):
        make_db(env.db, standard_rows())
        assert env.run(FakeClient(answer(verdict="proposals", proposals=[SHORTS_PROPOSAL]))) == 0
        text = env.report()
        assert "Advisory only" in text
        assert "- SIGNAL_SHORT_MAX_DEPTH=2\n+ SIGNAL_SHORT_MAX_DEPTH=1   # depth=2: n=60" in text
        assert "Verify first: `python backtest.py --days 60`" in text
        assert "**Verdict:** proposals" in text

    def test_all_proposals_rejected_reads_as_no_action(self, env):
        make_db(env.db, standard_rows())
        bad = dict(SHORTS_PROPOSAL, bucket_ids=["depth=1"])
        env.run(FakeClient(answer(verdict="proposals", proposals=[bad])))
        text = env.report()
        assert "no action (every proposal was rejected" in text
        assert "### Rejected by guardrail" in text
        assert "```diff" not in text

    def test_model_prose_is_labelled_as_its_reading(self, env):
        make_db(env.db, standard_rows())
        env.run(FakeClient(answer(
            summary="Depth-2 entries lose.",
            negative_buckets=[{"bucket_id": "depth=2", "interpretation": "deep pullbacks fail"}],
            loss_clusters=[{"description": "late deep entries", "bucket_ids": ["depth=2"],
                            "alert_ids": ["a", "b"]}],
            hypotheses=["Skip depth 2 after 20:00 UTC"], caveats=["costs not modelled"],
        )))
        text = env.report()
        for heading in ("## Summary (model's reading)", "## Negative buckets (model's reading)",
                        "## Loss clusters (model's reading)",
                        "## Hypotheses needing code (backtest first)",
                        "## Caveats (model's reading)"):
            assert heading in text
        assert "(buckets `depth=2`; 2 alerts)" in text

    def test_cost_line(self, env):
        make_db(env.db, standard_rows())
        env.run(FakeClient(answer()))
        assert "20000 input / 3000 output tokens, about $0.14" in env.report()

    def test_data_quality_counts(self, env):
        rows = standard_rows()
        rows[1]["exit_gapped"] = 1
        rows.append(alert(200, 1, True, outcome="expired", r=None, alert_id="exp"))
        make_db(env.db, rows)
        conn = alert_log.connect(env.db)   # ingest sets this, not insert
        conn.execute("UPDATE alerts SET duplicate_sends = 1 WHERE alert_id = ?",
                     (rows[0]["alert_id"],))
        conn.commit()
        conn.close()
        env.run(no_llm=True)
        text = env.report()
        assert "Bars alerted more than once: 1" in text
        assert "gapped through the level (real fill worse): 1" in text
        assert "expired: 1" in text

    def test_backtest_prior_is_shown_separately(self, env):
        rows = standard_rows() + [dict(alert(i, 1, True), source="backtest:2026-10-01")
                                  for i in range(40)]
        make_db(env.db, rows)
        env.run(no_llm=True)
        assert "## Backtest prior (backtest:2026-10-01)" in env.report()


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

class TestFailures:
    @pytest.mark.parametrize("client,why", [
        (FakeClient("{not json"), "not JSON"),
        (FakeClient('["a list"]'), "not a JSON object"),
        (FakeClient({"verdict": "maybe"}), "did not fit the schema"),
        (FakeClient(answer(), stop_reason="refusal"), "declined"),
        (FakeClient(answer(), stop_reason="max_tokens"), "cut off"),
        (FakeClient(raises=ConnectionError("network down")), "network down"),
    ])
    def test_any_failure_writes_a_report_and_exits_1(self, env, client, why):
        make_db(env.db, standard_rows())
        assert env.run(client) == 1
        text = env.report()
        assert "## Review failed" in text
        assert why in text
        assert "| `depth=2` | 60 |" in text      # the stats are still there

    def test_secrets_never_reach_the_report(self, env, monkeypatch):
        key = "sk-ant-test-0123456789"
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
        make_db(env.db, standard_rows())
        env.run(FakeClient(raises=RuntimeError(
            f"auth failed for {key} posting to {signal_bot.DISCORD_WEBHOOK_URL}")))
        text = env.report()
        assert key not in text
        assert signal_bot.DISCORD_WEBHOOK_URL not in text


class TestDiscord:
    def test_summary_is_posted_when_asked(self, env, monkeypatch):
        posted = []
        monkeypatch.setattr(review.requests, "post",
                            lambda url, json=None, timeout=None: posted.append(json) or
                            SimpleNamespace(raise_for_status=lambda: None))
        make_db(env.db, standard_rows())
        env.run(no_llm=True, post=True)
        (body,) = posted
        assert "Weekly alert review 2026-10-06" in body["content"]
        assert "Advisory only" in body["content"]
        assert len(body["content"]) <= 1900

    def test_a_failed_post_does_not_fail_the_review(self, env, monkeypatch, capsys):
        def boom(*a, **k):
            raise review.requests.ConnectionError(f"down {signal_bot.DISCORD_WEBHOOK_URL}")
        monkeypatch.setattr(review.requests, "post", boom)
        make_db(env.db, standard_rows())
        assert env.run(no_llm=True, post=True) == 0
        out = capsys.readouterr().out
        assert "Discord summary not posted" in out
        assert signal_bot.DISCORD_WEBHOOK_URL not in out

    def test_not_posted_by_default(self, env, monkeypatch):
        monkeypatch.setattr(review.requests, "post", lambda *a, **k: pytest.fail("posted"))
        make_db(env.db, standard_rows())
        env.run(no_llm=True)


class TestSeparation:
    def test_the_bot_and_dashboard_never_load_the_reviewer_or_the_sdk(self):
        code = ("import sys, signal_bot, dashboard.app; "
                "assert 'review' not in sys.modules; assert 'anthropic' not in sys.modules")
        subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, check=True)
