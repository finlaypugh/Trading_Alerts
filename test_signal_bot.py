"""
Unit tests for signal_bot.py (Williams Fractals + Triple EMA version).

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


def make_hl_df(highs, lows=None):
    """OHLC frame where High/Low are given explicitly (for fractal tests)."""
    highs = pd.Series(highs, dtype=float)
    lows = pd.Series(lows, dtype=float) if lows is not None else highs - 1.0
    return pd.DataFrame({"High": highs, "Low": lows, "Close": (highs + lows) / 2})


SIGNAL_COLS = ["Close", "fractal_high", "fractal_low", "ema_1", "ema_2", "ema_3", "atr"]

# Neutral baseline: no fractal levels, flat EMAs, non-zero ATR.
SIGNAL_BASE = {
    "Close": 100.0,
    "fractal_high": float("nan"),
    "fractal_low": float("nan"),
    "ema_1": 100.0,
    "ema_2": 100.0,
    "ema_3": 100.0,
    "atr": 10.0,
}


def make_signal_df(n_rows, prev, curr, filler=None):
    """
    Build a DataFrame with pre-set Close/fractal_high/fractal_low/ema_*/atr
    columns so detect_signal() can be tested independently of
    compute_indicators(). Only the last two rows (prev, curr) drive the
    breakout logic; filler rows exist to satisfy MIN_BARS and to give
    ema_1 a defined value 10 bars back for the slope component.
    """
    base = dict(SIGNAL_BASE)
    base.update(filler or {})

    rows = [dict(base) for _ in range(n_rows - 2)]
    rows.append({c: prev.get(c, base[c]) for c in SIGNAL_COLS})
    rows.append({c: curr.get(c, base[c]) for c in SIGNAL_COLS})

    return pd.DataFrame(rows, columns=SIGNAL_COLS)


# Break above a fractal high of 100: prev sits on it, curr closes through.
BUY_PREV = {"fractal_high": 100.0, "Close": 99.0}
BUY_CURR = {"fractal_high": 100.0, "Close": 105.0}

SELL_PREV = {"fractal_low": 100.0, "Close": 101.0}
SELL_CURR = {"fractal_low": 100.0, "Close": 95.0}

# EMA regimes applied to the current bar. A regime depends on where the close
# sits relative to ema_1, so each stack is paired with the break it goes with:
# BULL_STACK sits under the BUY close of 105, BEAR_STACK over the SELL close
# of 95. The _CONTRA variants are the same regimes positioned so they oppose
# the break instead of confirming it.
BULL_STACK = {"ema_1": 102.0, "ema_2": 101.0, "ema_3": 100.0}
BEAR_STACK = {"ema_1": 98.0, "ema_2": 99.0, "ema_3": 100.0}
BEAR_STACK_CONTRA = {"ema_1": 110.0, "ema_2": 120.0, "ema_3": 130.0}
BULL_STACK_CONTRA = {"ema_1": 90.0, "ema_2": 85.0, "ema_3": 80.0}


# ---------------------------------------------------------------------------
# compute_indicators
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    EXPECTED_COLUMNS = {
        "ema_1", "ema_2", "ema_3",
        "up_fractal", "down_fractal", "fractal_high", "fractal_low",
        "atr",
    }

    def test_adds_expected_columns(self):
        df = make_ohlc_df([100 + i for i in range(60)])
        out = signal_bot.compute_indicators(df)
        assert self.EXPECTED_COLUMNS.issubset(out.columns)

    def test_does_not_mutate_input(self):
        df = make_ohlc_df([100 + i for i in range(60)])
        original = df.copy()
        signal_bot.compute_indicators(df)
        pd.testing.assert_frame_equal(df, original)

    def test_atr_is_non_negative(self):
        closes = ([100, 102, 99, 105, 103, 108, 101, 110, 107, 112] * 6)
        out = signal_bot.compute_indicators(make_ohlc_df(closes))
        assert (out["atr"].dropna() >= 0).all()

    def test_rising_series_gives_bullish_ema_stack(self):
        out = signal_bot.compute_indicators(make_ohlc_df([100 + i for i in range(600)]))
        last = out.iloc[-1]
        assert last["ema_1"] > last["ema_2"] > last["ema_3"]

    def test_falling_series_gives_bearish_ema_stack(self):
        out = signal_bot.compute_indicators(make_ohlc_df([1000 - i for i in range(600)]))
        last = out.iloc[-1]
        assert last["ema_1"] < last["ema_2"] < last["ema_3"]


# ---------------------------------------------------------------------------
# Williams fractals
# ---------------------------------------------------------------------------

class TestFractals:
    N = 2

    def _mask(self, values, up=True, max_plateau=4):
        series = pd.Series(values, dtype=float)
        return signal_bot._fractal_mask(series, self.N, max_plateau, up=up)

    def test_clean_up_fractal(self):
        # Pivot at index 4: two strictly lower bars either side.
        highs = [10, 11, 12, 13, 20, 13, 12, 11, 10]
        mask = self._mask(highs, up=True)
        assert bool(mask.iloc[4])
        assert not mask.iloc[3]
        assert not mask.iloc[5]

    def test_clean_down_fractal(self):
        lows = [20, 19, 18, 17, 5, 17, 18, 19, 20]
        mask = self._mask(lows, up=False)
        assert bool(mask.iloc[4])
        assert not mask.iloc[3]
        assert not mask.iloc[5]

    def test_plateau_to_the_left_still_registers(self):
        # Two equal highs immediately left of the pivot at index 6.
        highs = [10, 11, 12, 13, 20, 20, 20, 13, 12, 11, 10]
        mask = self._mask(highs, up=True, max_plateau=4)
        assert bool(mask.iloc[6])

    def test_plateau_beyond_allowance_does_not_register(self):
        # Three equal highs to the left, but only one is tolerated.
        highs = [10, 11, 12, 13, 20, 20, 20, 20, 13, 12, 11, 10]
        mask = self._mask(highs, up=True, max_plateau=1)
        assert not mask.iloc[7]

    def test_no_fractal_when_right_side_not_strictly_lower(self):
        # An equal high to the right kills it; only the left side plateaus.
        highs = [10, 11, 12, 13, 20, 20, 12, 11, 10]
        mask = self._mask(highs, up=True)
        assert not mask.iloc[4]

    def test_fractal_level_is_not_visible_until_n_bars_later(self):
        """
        The lookahead regression test. A pivot at bar p must not be readable
        before bar p + n, or the bot trades on information it could not have
        had at the time.
        """
        highs = [10, 11, 12, 13, 20, 13, 12, 11, 10, 9, 8, 7]
        df = make_hl_df(highs, lows=[h - 5 for h in highs])
        out = signal_bot.compute_indicators(df)

        pivot = 4
        n = signal_bot.FRACTAL_N
        assert bool(out["up_fractal"].iloc[pivot])
        # Still unknown at the pivot itself and for the next n-1 bars.
        for i in range(pivot, pivot + n):
            assert pd.isna(out["fractal_high"].iloc[i]), f"leaked at bar {i}"
        # Confirmed exactly n bars after the pivot.
        assert out["fractal_high"].iloc[pivot + n] == pytest.approx(20.0)

    def test_fractal_level_forward_fills_after_confirmation(self):
        highs = [10, 11, 12, 13, 20, 13, 12, 11, 10, 9, 8, 7]
        df = make_hl_df(highs, lows=[h - 5 for h in highs])
        out = signal_bot.compute_indicators(df)
        assert out["fractal_high"].iloc[-1] == pytest.approx(20.0)


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

class TestDetectSignal:
    def test_returns_none_when_fewer_than_min_bars(self):
        df = make_signal_df(signal_bot.MIN_BARS - 1, BUY_PREV, dict(BUY_CURR, **BULL_STACK))
        signal, strength, tier, reason = signal_bot.detect_signal(df)
        assert (signal, strength, tier, reason) == (None, 0.0, None, "")

    def test_strong_buy_on_break_inside_bullish_stack(self):
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, dict(BUY_CURR, **BULL_STACK))
        signal, strength, tier, reason = signal_bot.detect_signal(df)
        assert (signal, tier) == ("BUY", "STRONG")
        assert 0.0 <= strength <= 1.0
        assert reason

    def test_strong_sell_on_break_inside_bearish_stack(self):
        df = make_signal_df(signal_bot.MIN_BARS, SELL_PREV, dict(SELL_CURR, **BEAR_STACK))
        signal, strength, tier, reason = signal_bot.detect_signal(df)
        assert (signal, tier) == ("SELL", "STRONG")
        assert 0.0 <= strength <= 1.0
        assert reason

    def test_weak_buy_when_ema_regime_is_neutral(self):
        # Flat EMAs (the base row) are neither a bull nor a bear stack.
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, BUY_CURR)
        signal, strength, tier, _ = signal_bot.detect_signal(df)
        assert (signal, tier) == ("BUY", "WEAK")

    def test_weak_sell_when_ema_regime_is_neutral(self):
        df = make_signal_df(signal_bot.MIN_BARS, SELL_PREV, SELL_CURR)
        signal, strength, tier, _ = signal_bot.detect_signal(df)
        assert (signal, tier) == ("SELL", "WEAK")

    def test_no_signal_when_buy_break_contradicts_bearish_stack(self):
        curr = dict(BUY_CURR, **BEAR_STACK_CONTRA)
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr)
        signal, strength, tier, reason = signal_bot.detect_signal(df)
        assert (signal, strength, tier, reason) == (None, 0.0, None, "")

    def test_no_signal_when_sell_break_contradicts_bullish_stack(self):
        curr = dict(SELL_CURR, **BULL_STACK_CONTRA)
        df = make_signal_df(signal_bot.MIN_BARS, SELL_PREV, curr)
        signal, strength, tier, reason = signal_bot.detect_signal(df)
        assert (signal, strength, tier, reason) == (None, 0.0, None, "")

    def test_no_signal_without_a_fractal_break(self):
        # Bullish stack, but price never crosses the level.
        prev = {"fractal_high": 100.0, "Close": 95.0}
        curr = dict(BULL_STACK, fractal_high=100.0, Close=96.0)
        df = make_signal_df(signal_bot.MIN_BARS, prev, curr)
        signal, strength, tier, reason = signal_bot.detect_signal(df)
        assert (signal, strength, tier, reason) == (None, 0.0, None, "")

    def test_no_signal_when_no_level_has_been_confirmed_yet(self):
        # fractal_high is NaN everywhere, so there is nothing to break.
        df = make_signal_df(signal_bot.MIN_BARS, {"Close": 99.0}, dict(BULL_STACK, Close=105.0))
        signal, _, tier, _ = signal_bot.detect_signal(df)
        assert signal is None and tier is None

    # Note: there is no test for both breaks firing on one bar. With
    # close-based triggers it is unreachable -- it would need the close both
    # above fractal_high and below fractal_low while the previous close sat
    # between them the other way round. detect_signal still guards against
    # it, since switching the trigger to intrabar High/Low would make it
    # reachable.

    def test_break_exactly_to_the_level_does_not_trigger(self):
        # Closing *at* the level is not through it.
        curr = dict(BULL_STACK, fractal_high=100.0, Close=100.0)
        df = make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr)
        signal, _, tier, _ = signal_bot.detect_signal(df)
        assert signal is None and tier is None


class TestStrength:
    def test_bigger_break_scores_higher(self):
        marginal = dict(BUY_CURR, Close=100.5, **BULL_STACK)
        decisive = dict(BUY_CURR, Close=105.0, **BULL_STACK)
        _, s_marginal, _, _ = signal_bot.detect_signal(
            make_signal_df(signal_bot.MIN_BARS, BUY_PREV, marginal)
        )
        _, s_decisive, _, _ = signal_bot.detect_signal(
            make_signal_df(signal_bot.MIN_BARS, BUY_PREV, decisive)
        )
        assert s_decisive > s_marginal

    def test_weak_signal_never_exceeds_the_cap(self):
        # A maximally decisive break, but with a neutral (flat) EMA stack.
        curr = dict(BUY_CURR, Close=1000.0)
        _, strength, tier, _ = signal_bot.detect_signal(
            make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr)
        )
        assert tier == "WEAK"
        assert strength <= signal_bot.WEAK_STRENGTH_CAP

    def test_zero_atr_falls_back_instead_of_dividing_by_zero(self):
        curr = dict(BUY_CURR, atr=0.0, **BULL_STACK)
        signal, strength, tier, _ = signal_bot.detect_signal(
            make_signal_df(signal_bot.MIN_BARS, BUY_PREV, curr, filler={"atr": 0.0})
        )
        assert (signal, tier) == ("BUY", "STRONG")
        assert strength == pytest.approx(0.5)


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
# State persistence
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
        signal_bot.save_last_signal("BUY", "WEAK")
        assert signal_bot.load_last_signal() == {"signal": "BUY", "tier": "WEAK"}

    def test_load_returns_none_on_corrupt_json(self, _isolate_state_file):
        _isolate_state_file.write_text("{not valid json")
        assert signal_bot.load_last_signal() is None

    def test_save_overwrites_previous_value(self, _isolate_state_file):
        signal_bot.save_last_signal("BUY", "WEAK")
        signal_bot.save_last_signal("SELL", "STRONG")
        assert signal_bot.load_last_signal() == {"signal": "SELL", "tier": "STRONG"}

    def test_bare_string_state_is_coerced_to_strong(self, _isolate_state_file):
        _isolate_state_file.write_text('"BUY"')
        assert signal_bot.load_last_signal() == {"signal": "BUY", "tier": "STRONG"}

    def test_dict_without_tier_defaults_to_strong(self, _isolate_state_file):
        _isolate_state_file.write_text('{"signal": "SELL"}')
        assert signal_bot.load_last_signal() == {"signal": "SELL", "tier": "STRONG"}

    def test_unrecognised_payload_returns_none(self, _isolate_state_file):
        _isolate_state_file.write_text('{"nothing": "useful"}')
        assert signal_bot.load_last_signal() is None


# ---------------------------------------------------------------------------
# send_discord_alert
# ---------------------------------------------------------------------------

class _FakeResponse:
    def raise_for_status(self):
        pass  # would raise on a real HTTP error; nothing to do here


ALERT_KWARGS = dict(
    signal="BUY", price=123.456, atr=3.2, strength=0.8, tier="STRONG",
    reason="Fractals(n=2) break above 120.00 + TEMA50/100/200 bullish stack",
    sl=118.0, tp=140.0, rr=2.5,
)


class TestSendDiscordAlert:
    def test_posts_expected_fields_in_content(self, monkeypatch):
        captured = {}

        def fake_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return _FakeResponse()

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        signal_bot.send_discord_alert(**ALERT_KWARGS)

        content = captured["json"]["content"]
        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "STRONG BUY" in content
        assert ALERT_KWARGS["reason"] in content
        assert "123.46" in content
        assert "SL: 118.00" in content
        assert "TP: 140.00" in content
        assert "1:2.50" in content
        assert captured["timeout"] == 10

    def test_weak_tier_is_labeled_in_content(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert(**dict(ALERT_KWARGS, tier="WEAK"))
        assert "WEAK BUY" in captured["json"]["content"]

    def test_does_not_accept_an_rsi_argument(self):
        with pytest.raises(TypeError):
            signal_bot.send_discord_alert(rsi=55.0, **ALERT_KWARGS)

    def test_sell_alert_uses_red_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert(
            "SELL", 100.0, 2.0, 0.5, "STRONG", "reason", 105.0, 90.0, 1.5
        )
        assert "\U0001F534" in captured["json"]["content"]

    def test_buy_alert_uses_green_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert(
            "BUY", 100.0, 2.0, 0.5, "STRONG", "reason", 95.0, 110.0, 1.5
        )
        assert "\U0001F7E2" in captured["json"]["content"]

    def test_propagates_http_errors(self, monkeypatch):
        class FailingResponse:
            def raise_for_status(self):
                raise signal_bot.requests.exceptions.HTTPError("boom")

        monkeypatch.setattr(
            signal_bot.requests, "post", lambda url, json, timeout: FailingResponse()
        )
        with pytest.raises(signal_bot.requests.exceptions.HTTPError):
            signal_bot.send_discord_alert(**ALERT_KWARGS)


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

        content = captured["json"]["content"]
        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "Signal bot started" in content
        assert "Williams Fractals" in content
        assert "Triple EMA" in content

    def test_swallows_request_exceptions_instead_of_raising(self, monkeypatch):
        def fake_post(url, json, timeout):
            raise signal_bot.requests.exceptions.ConnectionError("no network")

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        # Should not raise -- startup shouldn't crash the whole bot.
        signal_bot.send_startup_message()
