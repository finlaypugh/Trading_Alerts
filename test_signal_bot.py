"""
Unit tests for signal_bot.py.

Run locally:
    pip install pytest pandas
    pytest test_signal_bot.py -v

These tests never hit the network: yfinance/requests calls in run_once()
and send_discord_alert() are exercised only through mocks/monkeypatch.
"""
import json

import pandas as pd
import pytest

import signal_bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_price_df(closes):
    """Build a minimal OHLC-style DataFrame from a list of close prices."""
    return pd.DataFrame({"Close": closes})


def make_signal_df(n_rows, prev_fast, prev_slow, curr_fast, curr_slow, curr_rsi):
    """
    Build a DataFrame with pre-set ema_fast/ema_slow/rsi columns so
    detect_signal() can be tested independently of compute_indicators().
    Only the last two rows matter; earlier rows are filler.
    """
    ema_fast = [100.0] * (n_rows - 2) + [prev_fast, curr_fast]
    ema_slow = [100.0] * (n_rows - 2) + [prev_slow, curr_slow]
    rsi = [50.0] * (n_rows - 2) + [50.0, curr_rsi]
    return pd.DataFrame({"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi})


# ---------------------------------------------------------------------------
# compute_indicators
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    def test_adds_expected_columns(self):
        df = make_price_df([100 + i for i in range(30)])
        out = signal_bot.compute_indicators(df)
        assert {"ema_fast", "ema_slow", "rsi"}.issubset(out.columns)

    def test_does_not_mutate_input(self):
        df = make_price_df([100 + i for i in range(30)])
        original = df.copy()
        signal_bot.compute_indicators(df)
        pd.testing.assert_frame_equal(df, original)

    def test_rsi_within_bounds(self):
        closes = [100, 102, 99, 105, 103, 108, 101, 110, 107, 112] * 3
        out = signal_bot.compute_indicators(make_price_df(closes))
        valid_rsi = out["rsi"].dropna()
        assert (valid_rsi >= 0).all() and (valid_rsi <= 100).all()

    def test_monotonic_increase_pushes_rsi_to_100(self):
        closes = [100 + i for i in range(30)]
        out = signal_bot.compute_indicators(make_price_df(closes))
        assert out["rsi"].iloc[-1] == pytest.approx(100.0)

    def test_monotonic_decrease_pushes_rsi_to_0(self):
        closes = [130 - i for i in range(30)]
        out = signal_bot.compute_indicators(make_price_df(closes))
        assert out["rsi"].iloc[-1] == pytest.approx(0.0)

    def test_ema_fast_tracks_price_more_closely_than_ema_slow(self):
        # After a sharp jump, the fast EMA should have moved further
        # toward the new price than the slow EMA.
        closes = [100] * 25 + [150]
        out = signal_bot.compute_indicators(make_price_df(closes))
        last = out.iloc[-1]
        assert abs(last["ema_fast"] - 150) < abs(last["ema_slow"] - 150)


# ---------------------------------------------------------------------------
# detect_signal
# ---------------------------------------------------------------------------

class TestDetectSignal:
    def test_returns_none_when_fewer_than_min_bars(self):
        df = make_signal_df(
            signal_bot.MIN_BARS - 1,
            prev_fast=99, prev_slow=100, curr_fast=101, curr_slow=100, curr_rsi=50,
        )
        assert signal_bot.detect_signal(df) is None

    def test_buy_on_upward_cross_with_rsi_in_range(self):
        df = make_signal_df(
            signal_bot.MIN_BARS,
            prev_fast=99, prev_slow=100, curr_fast=101, curr_slow=100, curr_rsi=55,
        )
        assert signal_bot.detect_signal(df) == "BUY"

    def test_no_buy_when_upward_cross_but_rsi_too_high(self):
        df = make_signal_df(
            signal_bot.MIN_BARS,
            prev_fast=99, prev_slow=100, curr_fast=101, curr_slow=100, curr_rsi=85,
        )
        assert signal_bot.detect_signal(df) is None

    def test_no_buy_when_upward_cross_but_rsi_too_low(self):
        df = make_signal_df(
            signal_bot.MIN_BARS,
            prev_fast=99, prev_slow=100, curr_fast=101, curr_slow=100, curr_rsi=20,
        )
        assert signal_bot.detect_signal(df) is None

    def test_sell_on_downward_cross_ignores_rsi(self):
        # RSI filter only applies to BUY signals per detect_signal().
        df = make_signal_df(
            signal_bot.MIN_BARS,
            prev_fast=101, prev_slow=100, curr_fast=99, curr_slow=100, curr_rsi=95,
        )
        assert signal_bot.detect_signal(df) == "SELL"

    def test_none_when_no_crossover(self):
        df = make_signal_df(
            signal_bot.MIN_BARS,
            prev_fast=105, prev_slow=100, curr_fast=106, curr_slow=100, curr_rsi=55,
        )
        assert signal_bot.detect_signal(df) is None

    def test_exact_equality_at_prev_bar_counts_as_cross_up(self):
        # prev_fast == prev_slow, then curr_fast > curr_slow.
        df = make_signal_df(
            signal_bot.MIN_BARS,
            prev_fast=100, prev_slow=100, curr_fast=101, curr_slow=100, curr_rsi=55,
        )
        assert signal_bot.detect_signal(df) == "BUY"


# ---------------------------------------------------------------------------
# State persistence (load_last_signal / save_last_signal)
# ---------------------------------------------------------------------------

class TestStatePersistence:
    @pytest.fixture(autouse=True)
    def _isolate_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / ".state_TEST.json"
        monkeypatch.setattr(signal_bot, "STATE_FILE", state_file)
        yield state_file

    def test_load_returns_none_when_file_missing(self, _isolate_state_file):
        assert signal_bot.load_last_signal() is None

    def test_save_then_load_round_trip(self, _isolate_state_file):
        signal_bot.save_last_signal("BUY")
        assert signal_bot.load_last_signal() == "BUY"

    def test_load_returns_none_on_corrupt_json(self, _isolate_state_file):
        _isolate_state_file.write_text("{not valid json")
        assert signal_bot.load_last_signal() is None

    def test_save_overwrites_previous_value(self, _isolate_state_file):
        signal_bot.save_last_signal("BUY")
        signal_bot.save_last_signal("SELL")
        assert signal_bot.load_last_signal() == "SELL"


# ---------------------------------------------------------------------------
# send_discord_alert
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self):
        self.raised = False

    def raise_for_status(self):
        self.raised = True  # would raise on a real HTTP error; nothing to do here


class TestSendDiscordAlert:
    def test_posts_to_webhook_url_with_expected_content(self, monkeypatch):
        captured = {}

        def fake_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        signal_bot.send_discord_alert("BUY", 123.456, 55.4)

        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "BUY" in captured["json"]["content"]
        assert "123.46" in captured["json"]["content"]
        assert "55.4" in captured["json"]["content"]
        assert captured["timeout"] == 10

    def test_sell_alert_uses_red_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert("SELL", 100.0, 45.0)
        assert "\U0001F534" in captured["json"]["content"]

    def test_buy_alert_uses_green_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert("BUY", 100.0, 45.0)
        assert "\U0001F7E2" in captured["json"]["content"]

    def test_propagates_http_errors(self, monkeypatch):
        class FailingResponse:
            def raise_for_status(self):
                raise signal_bot.requests.exceptions.HTTPError("boom")

        monkeypatch.setattr(
            signal_bot.requests, "post", lambda url, json, timeout: FailingResponse()
        )
        with pytest.raises(signal_bot.requests.exceptions.HTTPError):
            signal_bot.send_discord_alert("BUY", 100.0, 45.0)
