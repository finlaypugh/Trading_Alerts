"""
Unit tests for signal_bot.py (dual-strategy confirmation version).

Run locally:
    pip install pytest pandas numpy
    pytest test_signal_bot.py -v

No network calls happen: yfinance/requests in run_once() and
send_discord_alert()/send_startup_message() are only exercised via mocks.
"""
import pandas as pd
import pytest

import signal_bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ohlc_df(closes, high_pad=1.0, low_pad=1.0):
    """Minimal OHLC DataFrame for compute_indicators()."""
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "High": closes + high_pad,
        "Low": closes - low_pad,
        "Close": closes,
    })


def make_signal_df(n_rows, prev, curr, filler=None):
    """
    Build a DataFrame with pre-set ema_fast/ema_slow/rsi/macd/macd_signal/
    macd_hist/atr columns so detect_signal() can be tested independently
    of compute_indicators(). Only the last two rows (prev, curr) matter
    for crossover detection; filler rows exist to satisfy MIN_BARS and
    give macd_hist some spread so its std isn't zero.
    """
    filler = filler or {}
    cols = ["ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "macd_hist", "atr"]
    base = {
        "ema_fast": 100.0, "ema_slow": 100.0, "rsi": 50.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0, "atr": 10.0,
    }
    base.update(filler)

    rows = []
    for i in range(n_rows - 2):
        row = dict(base)
        # alternate macd_hist slightly so tail(50).std() is non-zero
        row["macd_hist"] = 0.05 if i % 2 == 0 else -0.05
        rows.append(row)

    rows.append({c: prev.get(c, base[c]) for c in cols})
    rows.append({c: curr.get(c, base[c]) for c in cols})

    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# compute_indicators
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    EXPECTED_COLUMNS = {"ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "macd_hist", "atr"}

    def test_adds_expected_columns(self):
        df = make_ohlc_df([100 + i for i in range(60)])
        out = signal_bot.compute_indicators(df)
        assert self.EXPECTED_COLUMNS.issubset(out.columns)

    def test_does_not_mutate_input(self):
        df = make_ohlc_df([100 + i for i in range(60)])
        original = df.copy()
        signal_bot.compute_indicators(df)
        pd.testing.assert_frame_equal(df, original)

    def test_rsi_within_bounds(self):
        closes = ([100, 102, 99, 105, 103, 108, 101, 110, 107, 112] * 6)
        out = signal_bot.compute_indicators(make_ohlc_df(closes))
        valid_rsi = out["rsi"].dropna()
        assert (valid_rsi >= 0).all() and (valid_rsi <= 100).all()

    def test_atr_is_non_negative(self):
        closes = ([100, 102, 99, 105, 103, 108, 101, 110, 107, 112] * 6)
        out = signal_bot.compute_indicators(make_ohlc_df(closes))
        assert (out["atr"].dropna() >= 0).all()

    def test_macd_hist_equals_macd_minus_signal(self):
        closes = [100 + i * 0.5 for i in range(60)]
        out = signal_bot.compute_indicators(make_ohlc_df(closes))
        diff = out["macd_hist"] - (out["macd"] - out["macd_signal"])
        assert (diff.abs() < 1e-9).all()

    def test_monotonic_increase_pushes_rsi_toward_100(self):
        closes = [100 + i for i in range(40)]
        out = signal_bot.compute_indicators(make_ohlc_df(closes))
        assert out["rsi"].iloc[-1] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# _clip01
# ---------------------------------------------------------------------------

class TestClip01:
    def test_clamps_below_zero(self):
        assert signal_bot._clip01(-5) == 0.0

    def test_clamps_above_one(self):
        assert signal_bot._clip01(5) == 1.0

    def test_passes_through_mid_range(self):
        assert signal_bot._clip01(0.42) == 0.42


# ---------------------------------------------------------------------------
# detect_signal
# ---------------------------------------------------------------------------

BUY_PREV = {"ema_fast": 99, "ema_slow": 100, "macd": -1, "macd_signal": 0}
BUY_CURR = {"ema_fast": 101, "ema_slow": 100, "rsi": 55, "macd": 1, "macd_signal": 0, "macd_hist": 1, "atr": 5}

SELL_PREV = {"ema_fast": 101, "ema_slow": 100, "macd": 1, "macd_signal": 0}
SELL_CURR = {"ema_fast": 99, "ema_slow": 100, "rsi": 30, "macd": -1, "macd_signal": 0, "macd_hist": -1, "atr": 5}


class TestDetectSignal:
    def test_returns_none_when_fewer_than_min_bars(self):
        df = make_signal_df(signal_bot.MIN_BARS - 1, BUY_PREV, BUY_CURR)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None
        assert strength == 0.0

    def test_buy_when_both_strategies_agree(self):
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, BUY_CURR)
        signal, strength = signal_bot.detect_signal(df)
        assert signal == "BUY"
        assert 0.0 <= strength <= 1.0

    def test_sell_when_both_strategies_agree(self):
        df = make_signal_df(signal_bot.MIN_BARS, SELL_PREV, SELL_CURR)
        signal, strength = signal_bot.detect_signal(df)
        assert signal == "SELL"
        assert 0.0 <= strength <= 1.0

    def test_none_when_only_ema_strategy_agrees(self):
        # EMA crosses up, but MACD was already above its signal (no cross).
        prev = dict(BUY_PREV, macd=1, macd_signal=0)
        curr = dict(BUY_CURR, macd=1.1, macd_signal=0, macd_hist=1.1)
        df = make_signal_df(signal_bot.MIN_BARS, prev, curr)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None
        assert strength == 0.0

    def test_none_when_only_macd_strategy_agrees(self):
        # MACD crosses up, but EMA fast was already above EMA slow (no cross).
        prev = dict(BUY_PREV, ema_fast=101, ema_slow=100)
        curr = dict(BUY_CURR, ema_fast=102, ema_slow=100)
        df = make_signal_df(signal_bot.MIN_BARS, prev, curr)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None
        assert strength == 0.0

    def test_no_buy_when_rsi_above_band(self):
        curr = dict(BUY_CURR, rsi=85)
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None
        assert strength == 0.0

    def test_no_buy_when_rsi_below_band(self):
        curr = dict(BUY_CURR, rsi=20)
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None
        assert strength == 0.0

    def test_no_sell_when_rsi_too_high(self):
        # rsi_ok_sell requires rsi < (100 - RSI_MIN) = 60
        curr = dict(SELL_CURR, rsi=65)
        df = make_signal_df(signal_bot.MIN_BARS, SELL_PREV, curr)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None
        assert strength == 0.0

    def test_no_signal_when_macd_hist_wrong_sign_for_buy(self):
        # MACD crossed up numerically, but histogram is non-positive.
        curr = dict(BUY_CURR, macd_hist=-0.1)
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr)
        signal, strength = signal_bot.detect_signal(df)
        assert signal is None

    def test_strength_higher_when_rsi_more_central(self):
        centered = dict(BUY_CURR, rsi=55)  # midpoint of 40-70 band
        edge = dict(BUY_CURR, rsi=41)      # near the band's edge
        df_centered = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, centered)
        df_edge = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, edge)

        _, strength_centered = signal_bot.detect_signal(df_centered)
        _, strength_edge = signal_bot.detect_signal(df_edge)
        assert strength_centered > strength_edge


# ---------------------------------------------------------------------------
# build_sl_tp
# ---------------------------------------------------------------------------

class TestBuildSlTp:
    def test_buy_stop_below_and_target_above_price(self):
        sl, tp, rr = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=0.0)
        assert sl < 100.0 < tp

    def test_sell_stop_above_and_target_below_price(self):
        sl, tp, rr = signal_bot.build_sl_tp("SELL", price=100.0, atr=2.0, strength=0.0)
        assert tp < 100.0 < sl

    def test_rr_at_min_strength_equals_tp_rr_min(self):
        _, _, rr = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=0.0)
        assert rr == pytest.approx(signal_bot.TP_RR_MIN)

    def test_rr_at_max_strength_equals_tp_rr_max(self):
        _, _, rr = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=1.0)
        assert rr == pytest.approx(signal_bot.TP_RR_MAX)

    def test_stronger_signal_gives_wider_take_profit(self):
        _, tp_weak, _ = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=0.1)
        _, tp_strong, _ = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=0.9)
        assert tp_strong > tp_weak

    def test_stop_distance_scales_with_atr_not_strength(self):
        sl_low, _, _ = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=0.0)
        sl_high, _, _ = signal_bot.build_sl_tp("BUY", price=100.0, atr=2.0, strength=1.0)
        assert sl_low == pytest.approx(sl_high)
        assert sl_low == pytest.approx(100.0 - signal_bot.SL_ATR_MULT * 2.0)


# ---------------------------------------------------------------------------
# State persistence (unchanged behavior, still worth covering)
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
    def raise_for_status(self):
        pass  # would raise on a real HTTP error; nothing to do here


class TestSendDiscordAlert:
    def test_posts_expected_fields_in_content(self, monkeypatch):
        captured = {}

        def fake_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        signal_bot.send_discord_alert(
            signal="BUY", price=123.456, rsi=55.4, atr=3.2, strength=0.8,
            sl=118.0, tp=140.0, rr=2.5,
        )

        content = captured["json"]["content"]
        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "BUY" in content
        assert "123.46" in content
        assert "SL: 118.00" in content
        assert "TP: 140.00" in content
        assert "1:2.50" in content
        assert captured["timeout"] == 10

    def test_sell_alert_uses_red_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert("SELL", 100.0, 45.0, 2.0, 0.5, 95.0, 90.0, 1.5)
        assert "\U0001F534" in captured["json"]["content"]

    def test_buy_alert_uses_green_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert("BUY", 100.0, 45.0, 2.0, 0.5, 95.0, 110.0, 1.5)
        assert "\U0001F7E2" in captured["json"]["content"]

    def test_propagates_http_errors(self, monkeypatch):
        class FailingResponse:
            def raise_for_status(self):
                raise signal_bot.requests.exceptions.HTTPError("boom")

        monkeypatch.setattr(
            signal_bot.requests, "post", lambda url, json, timeout: FailingResponse()
        )
        with pytest.raises(signal_bot.requests.exceptions.HTTPError):
            signal_bot.send_discord_alert("BUY", 100.0, 45.0, 2.0, 0.5, 95.0, 110.0, 1.5)


# ---------------------------------------------------------------------------
# send_startup_message
# ---------------------------------------------------------------------------

class TestSendStartupMessage:
    def test_posts_startup_content(self, monkeypatch):
        captured = {}

        def fake_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        signal_bot.send_startup_message()

        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "Signal bot started" in captured["json"]["content"]

    def test_swallows_request_exceptions_instead_of_raising(self, monkeypatch):
        def fake_post(url, json, timeout):
            raise signal_bot.requests.exceptions.ConnectionError("no network")

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        # Should not raise -- startup shouldn't crash the whole bot.
        signal_bot.send_startup_message()
