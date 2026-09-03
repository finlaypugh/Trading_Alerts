"""
Unit tests for signal_bot.py (fractal pullback + triple EMA version).

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

def bars(n, start="2026-01-05 00:00", freq="15min", tz=None):
    """A DatetimeIndex of n evenly spaced bars."""
    return pd.date_range(start=start, periods=n, freq=freq, tz=tz)


def make_ohlc_df(closes, high_pad=1.0, low_pad=1.0, index=None):
    """Minimal OHLC DataFrame for compute_indicators().

    Column values are passed as plain arrays: handing pandas a Series with a
    RangeIndex alongside a DatetimeIndex silently reindexes everything to NaN.
    """
    closes = pd.Series(closes, dtype=float).to_numpy()
    idx = index if index is not None else bars(len(closes))
    return pd.DataFrame(
        {"High": closes + high_pad, "Low": closes - low_pad, "Close": closes},
        index=idx,
    )


def make_hl_df(highs, lows=None, index=None):
    """OHLC frame where High/Low are given explicitly (for fractal tests)."""
    highs = pd.Series(highs, dtype=float).to_numpy()
    lows = pd.Series(lows, dtype=float).to_numpy() if lows is not None else highs - 1.0
    idx = index if index is not None else bars(len(highs))
    return pd.DataFrame(
        {"High": highs, "Low": lows, "Close": (highs + lows) / 2}, index=idx
    )


def make_trend_df(tail, highs=None, lows=None, base_len=250, rising=True):
    """A long, clean trend with a hand-written tail bolted onto the end.

    The base leg is a straight line, which settles the EMAs into a wide stack
    with price well clear of the fast one; the tail is where the pullback and
    the fractal are staged. `highs`/`lows` override individual bars' wicks,
    keyed by index within the tail -- the only way to put a swing point on a
    bar whose close is on the other side of an EMA.
    """
    base = [100.0 + i for i in range(base_len)] if rising else [1000.0 - i for i in range(base_len)]
    closes = base + [float(c) for c in tail]
    lo = [c - 1.0 for c in closes]
    hi = [c + 1.0 for c in closes]
    for i, v in (lows or {}).items():
        lo[base_len + i] = float(v)
    for i, v in (highs or {}).items():
        hi[base_len + i] = float(v)
    return pd.DataFrame(
        {"High": hi, "Low": lo, "Close": closes}, index=bars(len(closes))
    )


SIGNAL_COLS = [
    "Close", "ema_fast", "ema_mid", "ema_slow", "atr",
    "green_arrow", "red_arrow",
    "long_depth", "short_depth", "long_vetoed", "short_vetoed",
    "long_pivot_depth", "short_pivot_depth",
    "bull_stack_bars", "bear_stack_bars",
    "session_gap",
]

# Neutral baseline: no arrows, flat EMAs, non-zero ATR.
SIGNAL_BASE = {
    "Close": 100.0,
    "ema_fast": 100.0, "ema_mid": 100.0, "ema_slow": 100.0,
    "atr": 10.0,
    "green_arrow": False, "red_arrow": False,
    "long_depth": 0, "short_depth": 0,
    "long_vetoed": False, "short_vetoed": False,
    "long_pivot_depth": 0, "short_pivot_depth": 0,
    "bull_stack_bars": 0, "bear_stack_bars": 0,
    "session_gap": False,
}

# Comfortably past any MIN_STACK_BARS a test sets, so the stack-stability gate
# stays out of the way of tests that are about something else.
SETTLED = 10

# A clean bull stack (fast above mid above slow) and its mirror.
BULL_STACK = {"ema_fast": 110.0, "ema_mid": 105.0, "ema_slow": 100.0}
BEAR_STACK = {"ema_fast": 90.0, "ema_mid": 95.0, "ema_slow": 100.0}


def make_signal_df(n_rows, curr, filler=None):
    """
    Frame with pre-set indicator columns so detect_signal() can be tested
    independently of compute_indicators(). Only the final row drives the
    decision; filler rows satisfy MIN_BARS and give ema_fast a value 10
    bars back for the slope component.
    """
    base = dict(SIGNAL_BASE)
    base.update(filler or {})
    rows = [dict(base) for _ in range(n_rows - 1)]
    rows.append({c: curr.get(c, base[c]) for c in SIGNAL_COLS})
    return pd.DataFrame(rows, columns=SIGNAL_COLS, index=bars(n_rows))


def make_pullback_df(closes, fast, mid, slow, gaps=None, atr=1.0):
    """Frame shaped for compute_pullback_state()."""
    n = len(closes)

    def col(v):
        return list(v) if isinstance(v, (list, tuple)) else [v] * n

    return pd.DataFrame(
        {
            "Close": [float(c) for c in closes],
            "ema_fast": [float(v) for v in col(fast)],
            "ema_mid": [float(v) for v in col(mid)],
            "ema_slow": [float(v) for v in col(slow)],
            "atr": [float(v) for v in col(atr)],
            "session_gap": [bool(v) for v in col(gaps if gaps is not None else False)],
        },
        index=bars(n),
    )


# ---------------------------------------------------------------------------
# interval_minutes
# ---------------------------------------------------------------------------

class TestIntervalMinutes:
    @pytest.mark.parametrize("text,expected", [
        ("15m", 15), ("1m", 1), ("5m", 5), ("1h", 60), ("4h", 240), ("1d", 1440),
    ])
    def test_parses_supported_suffixes(self, text, expected):
        assert signal_bot.interval_minutes(text) == expected

    def test_uses_module_interval_by_default(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "INTERVAL", "1h")
        assert signal_bot.interval_minutes() == 60

    @pytest.mark.parametrize("text", ["", "m", "15", "15x", "abc", "1.5m", "-5m"])
    def test_rejects_garbage(self, text):
        with pytest.raises(ValueError):
            signal_bot.interval_minutes(text)


# ---------------------------------------------------------------------------
# Closed-candle enforcement
# ---------------------------------------------------------------------------

class TestDropUnclosedBar:
    def _frame(self, last_open, n=5, tz=None):
        idx = pd.date_range(end=last_open, periods=n, freq="15min", tz=tz)
        return pd.DataFrame({"Close": range(n)}, index=idx)

    def test_drops_a_still_open_final_bar(self):
        # Opened one minute ago, so it has 14 minutes left to run.
        now = pd.Timestamp.now("UTC").tz_localize(None)
        df = self._frame(now - pd.Timedelta(minutes=1))
        out = signal_bot.drop_unclosed_bar(df)
        assert len(out) == len(df) - 1
        assert out.index[-1] == df.index[-2]

    def test_keeps_a_final_bar_that_has_closed(self):
        now = pd.Timestamp.now("UTC").tz_localize(None)
        df = self._frame(now - pd.Timedelta(minutes=30))
        out = signal_bot.drop_unclosed_bar(df)
        assert len(out) == len(df)

    def test_handles_tz_aware_index(self):
        now = pd.Timestamp.now(tz="UTC")
        df = self._frame(now - pd.Timedelta(minutes=1), tz="UTC")
        assert len(signal_bot.drop_unclosed_bar(df)) == len(df) - 1

    def test_disabled_by_config(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "DROP_UNCLOSED_BAR", False)
        now = pd.Timestamp.now("UTC").tz_localize(None)
        df = self._frame(now - pd.Timedelta(minutes=1))
        assert len(signal_bot.drop_unclosed_bar(df)) == len(df)

    def test_empty_frame_is_returned_unchanged(self):
        df = pd.DataFrame({"Close": []})
        assert signal_bot.drop_unclosed_bar(df).empty


# ---------------------------------------------------------------------------
# Session gaps
# ---------------------------------------------------------------------------

class TestSessionGaps:
    def test_flags_a_sixty_minute_break_in_a_15m_series(self):
        idx = list(bars(4)) + list(bars(3, start="2026-01-05 01:45"))
        df = pd.DataFrame({"Close": range(7)}, index=pd.DatetimeIndex(idx))
        out = signal_bot.mark_session_gaps(df)
        # bars() gives 00:00,00:15,00:30,00:45 then 01:45 -> a 60-minute break.
        assert not out["session_gap"].iloc[:4].any()
        assert bool(out["session_gap"].iloc[4])
        assert not out["session_gap"].iloc[5:].any()

    def test_regular_spacing_flags_nothing(self):
        df = pd.DataFrame({"Close": range(10)}, index=bars(10))
        out = signal_bot.mark_session_gaps(df)
        assert not out["session_gap"].any()

    def test_does_not_mutate_input(self):
        df = make_hl_df([10, 11, 12, 13, 14])
        original = df.copy()
        signal_bot.mark_session_gaps(df)
        pd.testing.assert_frame_equal(df, original)

    def test_pivot_whose_window_spans_a_gap_is_cleared(self):
        # A clean up-fractal at index 4, but a weekend gap sits at index 5.
        highs = [10, 11, 12, 13, 20, 13, 12, 11, 10, 9, 8, 7]
        idx = list(bars(5)) + list(bars(7, start="2026-01-07 00:00"))
        df = make_hl_df(highs, lows=[h - 5 for h in highs], index=pd.DatetimeIndex(idx))
        out = signal_bot.compute_indicators(df)
        assert not bool(out["up_fractal"].iloc[4])

    def test_same_pivot_survives_without_the_gap(self):
        highs = [10, 11, 12, 13, 20, 13, 12, 11, 10, 9, 8, 7]
        df = make_hl_df(highs, lows=[h - 5 for h in highs])
        out = signal_bot.compute_indicators(df)
        assert bool(out["up_fractal"].iloc[4])

    def test_gap_resets_pullback_depth_and_veto(self):
        # Bull stack throughout; a pullback below the mid EMA sets depth 2 and
        # a dip below the slow EMA vetoes, then a gap wipes both.
        closes = [110, 96, 94, 110, 110]
        df = make_pullback_df(closes, fast=105, mid=100, slow=95,
                              gaps=[False, False, False, True, False])
        out = signal_bot.compute_pullback_state(df)
        assert out["long_depth"].iloc[2] == 2
        assert bool(out["long_vetoed"].iloc[2])
        assert out["long_depth"].iloc[3] == 0
        assert not bool(out["long_vetoed"].iloc[3])


# ---------------------------------------------------------------------------
# Williams fractals and arrow mapping
# ---------------------------------------------------------------------------

class TestFractals:
    N = 2

    def _mask(self, values, up=True, max_plateau=4):
        series = pd.Series(values, dtype=float)
        return signal_bot._fractal_mask(series, self.N, max_plateau, up=up)

    def test_clean_up_fractal(self):
        highs = [10, 11, 12, 13, 20, 13, 12, 11, 10]
        mask = self._mask(highs, up=True)
        assert bool(mask.iloc[4])
        assert not mask.iloc[3] and not mask.iloc[5]

    def test_clean_down_fractal(self):
        lows = [20, 19, 18, 17, 5, 17, 18, 19, 20]
        mask = self._mask(lows, up=False)
        assert bool(mask.iloc[4])
        assert not mask.iloc[3] and not mask.iloc[5]

    def test_plateau_of_two_registers(self):
        highs = [10, 11, 12, 13, 20, 20, 20, 13, 12, 11, 10]
        assert bool(self._mask(highs, up=True, max_plateau=4).iloc[6])

    def test_plateau_of_five_does_not_register(self):
        highs = [10, 11, 12, 13, 20, 20, 20, 20, 20, 20, 13, 12, 11, 10]
        assert not self._mask(highs, up=True, max_plateau=4).iloc[9]

    def test_green_arrow_maps_to_the_swing_low(self):
        """
        The inversion trap. Green must follow the DOWN fractal (swing low,
        the long trigger); red must follow the UP fractal (swing high).
        Swapping these inverts every signal the bot produces.
        """
        # A clear V bottom at index 4 and nothing resembling a swing high.
        lows = [20, 19, 18, 17, 5, 17, 18, 19, 20, 21, 22, 23]
        highs = [v + 1 for v in lows]
        out = signal_bot.compute_indicators(make_hl_df(highs, lows=lows))
        n = signal_bot.FRACTAL_N

        assert bool(out["down_fractal"].iloc[4]), "expected a swing low at 4"
        assert bool(out["green_arrow"].iloc[4 + n]), "green must follow the swing low"
        assert not bool(out["red_arrow"].iloc[4 + n]), "red must not fire on a swing low"

    def test_red_arrow_maps_to_the_swing_high(self):
        highs = [10, 11, 12, 13, 30, 13, 12, 11, 10, 9, 8, 7]
        lows = [v - 1 for v in highs]
        out = signal_bot.compute_indicators(make_hl_df(highs, lows=lows))
        n = signal_bot.FRACTAL_N

        assert bool(out["up_fractal"].iloc[4]), "expected a swing high at 4"
        assert bool(out["red_arrow"].iloc[4 + n]), "red must follow the swing high"
        assert not bool(out["green_arrow"].iloc[4 + n]), "green must not fire on a swing high"

    def test_arrow_is_not_visible_until_n_bars_after_the_pivot(self):
        """
        The lookahead regression test. TradingView draws the arrow back at the
        pivot; a live bot cannot know it until n more bars have closed.
        """
        lows = [20, 19, 18, 17, 5, 17, 18, 19, 20, 21, 22, 23]
        out = signal_bot.compute_indicators(make_hl_df([v + 1 for v in lows], lows=lows))
        pivot, n = 4, signal_bot.FRACTAL_N

        assert bool(out["down_fractal"].iloc[pivot])
        for i in range(pivot, pivot + n):
            assert not bool(out["green_arrow"].iloc[i]), f"arrow leaked at bar {i}"
        assert bool(out["green_arrow"].iloc[pivot + n])


# ---------------------------------------------------------------------------
# compute_indicators
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    EXPECTED_COLUMNS = {
        "ema_fast", "ema_mid", "ema_slow", "atr", "session_gap",
        "up_fractal", "down_fractal", "green_arrow", "red_arrow",
        "long_depth", "short_depth", "long_vetoed", "short_vetoed",
        "long_pivot_depth", "short_pivot_depth",
        "bull_stack_bars", "bear_stack_bars",
    }

    def test_adds_expected_columns(self):
        out = signal_bot.compute_indicators(make_ohlc_df([100 + i for i in range(60)]))
        assert self.EXPECTED_COLUMNS.issubset(out.columns)

    def test_does_not_mutate_input(self):
        df = make_ohlc_df([100 + i for i in range(60)])
        original = df.copy()
        signal_bot.compute_indicators(df)
        pd.testing.assert_frame_equal(df, original)

    def test_atr_is_non_negative(self):
        closes = [100, 102, 99, 105, 103, 108, 101, 110, 107, 112] * 6
        out = signal_bot.compute_indicators(make_ohlc_df(closes))
        assert (out["atr"].dropna() >= 0).all()

    def test_rising_series_gives_bull_stack(self):
        out = signal_bot.compute_indicators(make_ohlc_df([100 + i for i in range(400)]))
        last = out.iloc[-1]
        assert last["ema_fast"] > last["ema_mid"] > last["ema_slow"]

    def test_falling_series_gives_bear_stack(self):
        out = signal_bot.compute_indicators(make_ohlc_df([1000 - i for i in range(400)]))
        last = out.iloc[-1]
        assert last["ema_fast"] < last["ema_mid"] < last["ema_slow"]


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
# Pullback state machine
# ---------------------------------------------------------------------------

class TestPullbackState:
    # Bull stack: fast 105, mid 100, slow 95.
    FAST, MID, SLOW = 105.0, 100.0, 95.0

    def test_close_below_fast_sets_depth_one(self):
        df = make_pullback_df([110, 103], self.FAST, self.MID, self.SLOW)
        out = signal_bot.compute_pullback_state(df)
        assert out["long_depth"].iloc[1] == 1
        assert not bool(out["long_vetoed"].iloc[1])

    def test_close_below_mid_sets_depth_two(self):
        df = make_pullback_df([110, 98], self.FAST, self.MID, self.SLOW)
        out = signal_bot.compute_pullback_state(df)
        assert out["long_depth"].iloc[1] == 2

    def test_close_below_slow_vetoes(self):
        df = make_pullback_df([110, 90], self.FAST, self.MID, self.SLOW)
        out = signal_bot.compute_pullback_state(df)
        assert bool(out["long_vetoed"].iloc[1])

    def test_depth_never_decreases_within_an_episode(self):
        # Dips to the mid, then only back to between mid and fast.
        df = make_pullback_df([110, 98, 103], self.FAST, self.MID, self.SLOW)
        out = signal_bot.compute_pullback_state(df)
        assert out["long_depth"].iloc[2] == 2

    def test_depth_resets_after_expiry_bars_back_above_fast(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "PULLBACK_EXPIRY_BARS", 3)
        closes = [110, 103, 110, 110, 110]
        out = signal_bot.compute_pullback_state(
            make_pullback_df(closes, self.FAST, self.MID, self.SLOW)
        )
        assert out["long_depth"].iloc[1] == 1
        assert out["long_depth"].iloc[3] == 1   # only 2 bars back above
        assert out["long_depth"].iloc[4] == 0   # 3rd bar closes the episode

    def test_losing_stack_alignment_resets_depth_and_veto(self):
        # Bull stack for two bars (pullback + veto), then the stack inverts.
        df = make_pullback_df(
            [110, 90, 90],
            fast=[105, 105, 90], mid=[100, 100, 95], slow=[95, 95, 100],
        )
        out = signal_bot.compute_pullback_state(df)
        assert bool(out["long_vetoed"].iloc[1])
        assert out["long_depth"].iloc[2] == 0
        assert not bool(out["long_vetoed"].iloc[2])

    def test_short_side_mirrors_the_long_side(self):
        # Bear stack: fast 95, mid 100, slow 105.
        df = make_pullback_df([90, 97, 102, 108], fast=95, mid=100, slow=105)
        out = signal_bot.compute_pullback_state(df)
        assert out["short_depth"].iloc[1] == 1   # above fast
        assert out["short_depth"].iloc[2] == 2   # above mid
        assert bool(out["short_vetoed"].iloc[3])  # above slow

    def test_does_not_mutate_input(self):
        df = make_pullback_df([110, 103], self.FAST, self.MID, self.SLOW)
        original = df.copy()
        signal_bot.compute_pullback_state(df)
        pd.testing.assert_frame_equal(df, original)


# ---------------------------------------------------------------------------
# detect_signal
# ---------------------------------------------------------------------------

BUY_CURR = dict(
    BULL_STACK, green_arrow=True, long_depth=1, long_pivot_depth=1,
    bull_stack_bars=SETTLED, Close=112.0,
)
SELL_CURR = dict(
    BEAR_STACK, red_arrow=True, short_depth=2, short_pivot_depth=2,
    bear_stack_bars=SETTLED, Close=88.0,
)


class TestDetectSignal:
    def test_returns_none_below_min_bars(self):
        df = make_signal_df(signal_bot.MIN_BARS - 1, BUY_CURR)
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_strong_buy_on_green_arrow_bull_stack_depth_one(self):
        df = make_signal_df(signal_bot.MIN_BARS, BUY_CURR)
        signal, strength, tier, depth, reason = signal_bot.detect_signal(df)
        assert (signal, tier, depth) == ("BUY", "STRONG", 1)
        assert 0.0 <= strength <= 1.0
        assert reason

    def test_strong_sell_on_red_arrow_bear_stack_depth_two(self):
        df = make_signal_df(signal_bot.MIN_BARS, SELL_CURR)
        signal, strength, tier, depth, reason = signal_bot.detect_signal(df)
        assert (signal, tier, depth) == ("SELL", "STRONG", 2)
        assert reason

    def test_veto_blocks_an_otherwise_valid_long(self):
        df = make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, long_vetoed=True))
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_veto_blocks_an_otherwise_valid_short(self):
        df = make_signal_df(signal_bot.MIN_BARS, dict(SELL_CURR, short_vetoed=True))
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_green_arrow_in_a_bear_stack_is_ignored(self):
        curr = dict(BEAR_STACK, green_arrow=True, long_depth=1, Close=90.0)
        assert signal_bot.detect_signal(make_signal_df(signal_bot.MIN_BARS, curr)) \
            == (None, 0.0, None, 0, "")

    def test_red_arrow_in_a_bull_stack_is_ignored(self):
        curr = dict(BULL_STACK, red_arrow=True, short_depth=1, Close=112.0)
        assert signal_bot.detect_signal(make_signal_df(signal_bot.MIN_BARS, curr)) \
            == (None, 0.0, None, 0, "")

    def test_no_arrow_means_no_signal(self):
        curr = dict(BULL_STACK, long_depth=1, Close=112.0)
        assert signal_bot.detect_signal(make_signal_df(signal_bot.MIN_BARS, curr)) \
            == (None, 0.0, None, 0, "")

    def test_depth_zero_is_suppressed_when_pullback_required(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "REQUIRE_PULLBACK", True)
        df = make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, long_depth=0))
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_depth_zero_is_weak_when_pullback_not_required(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "REQUIRE_PULLBACK", False)
        df = make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, long_depth=0))
        signal, strength, tier, depth, _ = signal_bot.detect_signal(df)
        assert (signal, tier, depth) == ("BUY", "WEAK", 0)
        assert strength <= signal_bot.WEAK_STRENGTH_CAP

    def test_shallow_pullback_scores_higher_than_a_deep_one(self):
        shallow = signal_bot.detect_signal(
            make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, long_depth=1))
        )[1]
        deep = signal_bot.detect_signal(
            make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, long_depth=2))
        )[1]
        assert shallow > deep

    def test_zero_atr_falls_back_instead_of_dividing_by_zero(self):
        df = make_signal_df(
            signal_bot.MIN_BARS, dict(BUY_CURR, atr=0.0), filler={"atr": 0.0}
        )
        signal, strength, tier, depth, _ = signal_bot.detect_signal(df)
        assert (signal, tier) == ("BUY", "STRONG")
        assert 0.0 <= strength <= 1.0


# ---------------------------------------------------------------------------
# Pivot-in-pullback gate
# ---------------------------------------------------------------------------

# Uptrend, then a pullback that closes below the fast EMA, with the swing low
# at the bottom of it. The green arrow lands on the final bar.
PIVOT_INSIDE = dict(tail=[340, 336, 334, 338, 342])

# Same uptrend, but the swing low is a wick two bars *before* price pulls back
# at all: the pivot bar closes above the fast EMA. Without the gate this fires,
# which is a reversal signal arriving ahead of the move it reverses.
PIVOT_BEFORE = dict(tail=[346, 344, 345, 336], lows={1: 332})

SHORT_PIVOT_INSIDE = dict(tail=[760, 764, 766, 762, 758], rising=False)
SHORT_PIVOT_BEFORE = dict(tail=[754, 756, 755, 764], highs={1: 768}, rising=False)


class TestPivotInPullback:
    """The video's sequence is pullback first, fractal at its low."""

    def test_pivot_inside_the_pullback_fires(self):
        out = signal_bot.compute_indicators(make_trend_df(**PIVOT_INSIDE))
        signal, _, tier, depth, _ = signal_bot.detect_signal(out)
        assert (signal, tier, depth) == ("BUY", "STRONG", 1)

    def test_pivot_formed_before_the_pullback_is_rejected(self):
        out = signal_bot.compute_indicators(make_trend_df(**PIVOT_BEFORE))
        assert bool(out["green_arrow"].iloc[-1])       # the trigger is there
        assert out["long_depth"].iloc[-1] == 1         # so is the pullback
        assert out["long_pivot_depth"].iloc[-1] == 0   # but not at the pivot
        assert signal_bot.detect_signal(out) == (None, 0.0, None, 0, "")

    def test_gate_off_restores_the_permissive_behaviour(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "REQUIRE_PIVOT_IN_PULLBACK", False)
        out = signal_bot.compute_indicators(make_trend_df(**PIVOT_BEFORE))
        assert signal_bot.detect_signal(out)[0] == "BUY"

    def test_gate_does_not_apply_when_pullbacks_are_optional(self, monkeypatch):
        # The WEAK path is already an acknowledged deviation; the gate has
        # nothing to say about a setup that never claimed to have a pullback.
        monkeypatch.setattr(signal_bot, "REQUIRE_PULLBACK", False)
        df = make_signal_df(
            signal_bot.MIN_BARS, dict(BUY_CURR, long_depth=0, long_pivot_depth=0)
        )
        signal, _, tier, depth, _ = signal_bot.detect_signal(df)
        assert (signal, tier, depth) == ("BUY", "WEAK", 0)

    def test_pivot_depth_is_not_visible_until_n_bars_after_the_pivot(self):
        """Same lookahead guarantee the arrows have: pivot depth is shifted."""
        out = signal_bot.compute_indicators(make_trend_df(**PIVOT_INSIDE))
        pivot = len(out) - 1 - signal_bot.FRACTAL_N

        assert bool(out["down_fractal"].iloc[pivot])
        for i in range(pivot, pivot + signal_bot.FRACTAL_N):
            assert out["long_pivot_depth"].iloc[i] == 0, f"leaked at bar {i}"
        assert out["long_pivot_depth"].iloc[pivot + signal_bot.FRACTAL_N] == 1

    def test_short_pivot_inside_the_pullback_fires(self):
        out = signal_bot.compute_indicators(make_trend_df(**SHORT_PIVOT_INSIDE))
        signal, _, tier, depth, _ = signal_bot.detect_signal(out)
        assert (signal, tier, depth) == ("SELL", "STRONG", 1)

    def test_short_pivot_formed_before_the_pullback_is_rejected(self):
        out = signal_bot.compute_indicators(make_trend_df(**SHORT_PIVOT_BEFORE))
        assert bool(out["red_arrow"].iloc[-1])
        assert out["short_depth"].iloc[-1] == 1
        assert out["short_pivot_depth"].iloc[-1] == 0
        assert signal_bot.detect_signal(out) == (None, 0.0, None, 0, "")


# ---------------------------------------------------------------------------
# Stack stability ("no trade" while the MAs are crossing)
# ---------------------------------------------------------------------------

class TestStackStability:
    FAST, MID, SLOW = 105.0, 100.0, 95.0

    def test_signal_suppressed_while_the_stack_is_still_fresh(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "MIN_STACK_BARS", 3)
        df = make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, bull_stack_bars=2))
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_exactly_min_stack_bars_is_enough(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "MIN_STACK_BARS", 3)
        df = make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, bull_stack_bars=3))
        assert signal_bot.detect_signal(df)[0] == "BUY"

    def test_short_side_is_gated_too(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "MIN_STACK_BARS", 3)
        df = make_signal_df(signal_bot.MIN_BARS, dict(SELL_CURR, bear_stack_bars=2))
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_min_stack_bars_of_one_reproduces_the_old_behaviour(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "MIN_STACK_BARS", 1)
        df = make_signal_df(signal_bot.MIN_BARS, dict(BUY_CURR, bull_stack_bars=1))
        assert signal_bot.detect_signal(df)[0] == "BUY"

    def test_counter_increments_while_the_stack_holds(self):
        df = make_pullback_df([110, 110, 110], self.FAST, self.MID, self.SLOW)
        out = signal_bot.compute_pullback_state(df)
        assert list(out["bull_stack_bars"]) == [1, 2, 3]

    def test_counter_resets_on_a_session_gap(self):
        df = make_pullback_df(
            [110, 110, 110, 110], self.FAST, self.MID, self.SLOW,
            gaps=[False, False, True, False],
        )
        out = signal_bot.compute_pullback_state(df)
        assert list(out["bull_stack_bars"]) == [1, 2, 0, 1]

    def test_counter_resets_when_the_stack_inverts(self):
        df = make_pullback_df(
            [110, 110, 90, 90],
            fast=[105, 105, 90, 90], mid=[100, 100, 95, 95], slow=[95, 95, 100, 100],
        )
        out = signal_bot.compute_pullback_state(df)
        assert list(out["bull_stack_bars"]) == [1, 2, 0, 0]
        assert list(out["bear_stack_bars"]) == [0, 0, 1, 2]


# ---------------------------------------------------------------------------
# Short-side depth cap
# ---------------------------------------------------------------------------

class TestShortDepthCap:
    def test_depth_two_short_is_suppressed_when_capped_at_one(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "SHORT_MAX_DEPTH", 1)
        df = make_signal_df(signal_bot.MIN_BARS, SELL_CURR)
        assert signal_bot.detect_signal(df) == (None, 0.0, None, 0, "")

    def test_depth_one_short_still_fires_when_capped_at_one(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "SHORT_MAX_DEPTH", 1)
        df = make_signal_df(
            signal_bot.MIN_BARS,
            dict(SELL_CURR, short_depth=1, short_pivot_depth=1),
        )
        assert signal_bot.detect_signal(df)[0] == "SELL"

    def test_the_cap_does_not_touch_the_long_side(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "SHORT_MAX_DEPTH", 1)
        df = make_signal_df(
            signal_bot.MIN_BARS,
            dict(BUY_CURR, long_depth=2, long_pivot_depth=2),
        )
        signal, _, _, depth, _ = signal_bot.detect_signal(df)
        assert (signal, depth) == ("BUY", 2)

    def test_default_of_two_mirrors_the_long_side(self):
        assert signal_bot.SHORT_MAX_DEPTH == 2
        df = make_signal_df(signal_bot.MIN_BARS, SELL_CURR)
        signal, _, _, depth, _ = signal_bot.detect_signal(df)
        assert (signal, depth) == ("SELL", 2)


# ---------------------------------------------------------------------------
# build_sl_tp
# ---------------------------------------------------------------------------

class TestBuildSlTp:
    def test_depth_one_long_stops_below_the_mid_ema(self):
        sl, tp, rr, risk = signal_bot.build_sl_tp(
            "BUY", price=112.0, depth=1, ema_mid=105.0, ema_slow=100.0, atr=4.0
        )
        assert sl == pytest.approx(105.0 - signal_bot.SL_BUFFER_ATR * 4.0)
        assert sl < 112.0 < tp

    def test_depth_two_long_stops_below_the_slow_ema(self):
        sl, _, _, _ = signal_bot.build_sl_tp(
            "BUY", price=112.0, depth=2, ema_mid=105.0, ema_slow=100.0, atr=4.0
        )
        assert sl == pytest.approx(100.0 - signal_bot.SL_BUFFER_ATR * 4.0)

    def test_depth_one_short_stops_above_the_mid_ema(self):
        sl, tp, _, _ = signal_bot.build_sl_tp(
            "SELL", price=88.0, depth=1, ema_mid=95.0, ema_slow=100.0, atr=4.0
        )
        assert sl == pytest.approx(95.0 + signal_bot.SL_BUFFER_ATR * 4.0)
        assert tp < 88.0 < sl

    def test_depth_two_short_stops_above_the_slow_ema(self):
        sl, _, _, _ = signal_bot.build_sl_tp(
            "SELL", price=88.0, depth=2, ema_mid=95.0, ema_slow=100.0, atr=4.0
        )
        assert sl == pytest.approx(100.0 + signal_bot.SL_BUFFER_ATR * 4.0)

    def test_target_is_exactly_rr_times_risk_for_a_long(self):
        price = 112.0
        sl, tp, rr, risk = signal_bot.build_sl_tp(
            "BUY", price=price, depth=1, ema_mid=105.0, ema_slow=100.0, atr=4.0
        )
        assert risk == pytest.approx(price - sl)
        assert tp == pytest.approx(price + signal_bot.RR * risk)
        assert rr == signal_bot.RR

    def test_target_is_exactly_rr_times_risk_for_a_short(self):
        price = 88.0
        sl, tp, _, risk = signal_bot.build_sl_tp(
            "SELL", price=price, depth=1, ema_mid=95.0, ema_slow=100.0, atr=4.0
        )
        assert risk == pytest.approx(sl - price)
        assert tp == pytest.approx(price - signal_bot.RR * risk)

    def test_returns_none_when_stop_lands_on_the_wrong_side_of_entry(self):
        # Long, but price is already below the mid EMA the stop references.
        assert signal_bot.build_sl_tp(
            "BUY", price=100.0, depth=1, ema_mid=105.0, ema_slow=95.0, atr=4.0
        ) is None

    def test_returns_none_for_a_short_below_its_stop_reference(self):
        assert signal_bot.build_sl_tp(
            "SELL", price=100.0, depth=1, ema_mid=95.0, ema_slow=105.0, atr=4.0
        ) is None

    def test_max_risk_ceiling_rejects_an_absurdly_wide_stop(self, monkeypatch):
        monkeypatch.setattr(signal_bot, "MAX_RISK_ATR", 2.0)
        # Risk here is ~7 ATRs, far beyond the ceiling.
        assert signal_bot.build_sl_tp(
            "BUY", price=112.0, depth=2, ema_mid=105.0, ema_slow=100.0, atr=1.5
        ) is None

    def test_max_risk_ceiling_off_by_default(self):
        assert signal_bot.MAX_RISK_ATR == 0.0
        assert signal_bot.build_sl_tp(
            "BUY", price=112.0, depth=2, ema_mid=105.0, ema_slow=100.0, atr=1.5
        ) is not None


# ---------------------------------------------------------------------------
# State persistence and cooldown
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
        signal_bot.save_last_signal("BUY", "STRONG", 2, "2026-01-05 12:00:00")
        assert signal_bot.load_last_signal() == {
            "signal": "BUY", "tier": "STRONG", "depth": 2,
            "bar_time": "2026-01-05 12:00:00",
        }

    def test_load_returns_none_on_corrupt_json(self, _isolate_state_file):
        _isolate_state_file.write_text("{not valid json")
        assert signal_bot.load_last_signal() is None

    def test_bare_string_state_is_coerced(self, _isolate_state_file):
        _isolate_state_file.write_text('"BUY"')
        assert signal_bot.load_last_signal() == {
            "signal": "BUY", "tier": "STRONG", "depth": 1, "bar_time": None,
        }

    def test_dict_without_optional_fields_gets_defaults(self, _isolate_state_file):
        _isolate_state_file.write_text('{"signal": "SELL"}')
        assert signal_bot.load_last_signal() == {
            "signal": "SELL", "tier": "STRONG", "depth": 1, "bar_time": None,
        }

    def test_unrecognised_payload_returns_none(self, _isolate_state_file):
        _isolate_state_file.write_text('{"nothing": "useful"}')
        assert signal_bot.load_last_signal() is None


class TestBarsSince:
    def test_counts_bars_after_the_stored_time(self):
        df = pd.DataFrame({"Close": range(10)}, index=bars(10))
        assert signal_bot.bars_since(df, df.index[6]) == 3

    def test_returns_none_without_a_stored_time(self):
        df = pd.DataFrame({"Close": range(10)}, index=bars(10))
        assert signal_bot.bars_since(df, None) is None

    def test_counts_actual_bars_not_wall_clock_across_a_gap(self):
        # Three bars, a weekend, then two more. Two bars follow the stored one
        # even though ~2 days of wall clock elapsed.
        idx = list(bars(3)) + list(bars(2, start="2026-01-07 00:00"))
        df = pd.DataFrame({"Close": range(5)}, index=pd.DatetimeIndex(idx))
        assert signal_bot.bars_since(df, df.index[2]) == 2

    def test_stored_time_older_than_the_frame_counts_everything(self):
        df = pd.DataFrame({"Close": range(10)}, index=bars(10))
        assert signal_bot.bars_since(df, "2020-01-01") == 10


class _RunOnceEnv:
    """Captures what run_once() did, without network or Discord."""

    def __init__(self, index):
        self.index = index
        self.sent = []
        # The final row of the frame run_once() reads its price and EMAs from.
        # Tests reassign this before calling run_once().
        self.curr = dict(BUY_CURR)

    def indicator_frame(self, df):
        return make_signal_df(len(df), self.curr).set_index(df.index)


@pytest.fixture
def run_once_env(tmp_path, monkeypatch):
    """Wire run_once() up against stubs, leaving its own logic intact."""
    monkeypatch.setattr(signal_bot, "STATE_FILE", tmp_path / ".state_TEST.json")
    monkeypatch.setattr(signal_bot, "COOLDOWN_BARS", 4)

    idx = bars(signal_bot.MIN_BARS)
    env = _RunOnceEnv(idx)

    monkeypatch.setattr(
        signal_bot, "send_discord_alert",
        lambda *a, **k: env.sent.append((a, k)),
    )
    raw = pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 112.0, "Volume": 0.0},
        index=idx,
    )
    monkeypatch.setattr(signal_bot.yf, "download", lambda *a, **k: raw.copy())
    monkeypatch.setattr(signal_bot, "drop_unclosed_bar", lambda df: df)
    monkeypatch.setattr(signal_bot, "compute_indicators", env.indicator_frame)
    return env


def detect_stub(signal="BUY", tier="STRONG", depth=1):
    return lambda df: (signal, 0.8, tier, depth, "test reason")


class TestCooldown:
    """run_once()'s throttle: same direction inside COOLDOWN_BARS is dropped."""

    def test_same_direction_within_cooldown_is_suppressed(self, run_once_env, monkeypatch):
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        # Stored two bars ago, cooldown is 4.
        signal_bot.save_last_signal("BUY", "STRONG", 1, str(run_once_env.index[-3]))
        signal_bot.run_once()
        assert run_once_env.sent == []

    def test_same_direction_after_cooldown_is_allowed(self, run_once_env, monkeypatch):
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        signal_bot.save_last_signal("BUY", "WEAK", 1, str(run_once_env.index[-10]))
        signal_bot.run_once()
        assert len(run_once_env.sent) == 1

    def test_opposite_direction_is_never_suppressed(self, run_once_env, monkeypatch):
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub(signal="BUY"))
        signal_bot.save_last_signal("SELL", "STRONG", 1, str(run_once_env.index[-2]))
        signal_bot.run_once()
        assert len(run_once_env.sent) == 1

    def test_no_signal_sends_nothing(self, run_once_env, monkeypatch):
        monkeypatch.setattr(
            signal_bot, "detect_signal", lambda df: (None, 0.0, None, 0, "")
        )
        signal_bot.run_once()
        assert run_once_env.sent == []


class TestRunOnceRefusedTrades:
    """
    A qualifying signal is not enough on its own: if build_sl_tp refuses the
    trade, run_once() must stay silent rather than alert with nonsense levels,
    and must not record state for an alert it never sent.

    These exercise the real build_sl_tp, not a stub -- test_premise_* pins the
    inputs that make it refuse, so the tests cannot silently stop testing the
    branch if those numbers drift.
    """

    # Depth-1 long: the stop references the mid EMA at 105 with a 0.25 x ATR(10)
    # buffer, putting it at 102.5. A close of 100 sits below its own stop.
    WRONG_SIDE = dict(BUY_CURR, Close=100.0)

    def test_premise_wrong_side_inputs_really_are_refused(self):
        assert signal_bot.build_sl_tp(
            "BUY", price=100.0, depth=1, ema_mid=105.0, ema_slow=100.0, atr=10.0
        ) is None

    def test_premise_valid_inputs_really_are_accepted(self):
        assert signal_bot.build_sl_tp(
            "BUY", price=112.0, depth=1, ema_mid=105.0, ema_slow=100.0, atr=10.0
        ) is not None

    def test_no_alert_when_the_stop_lands_on_the_wrong_side_of_entry(
        self, run_once_env, monkeypatch
    ):
        run_once_env.curr = self.WRONG_SIDE
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        signal_bot.run_once()
        assert run_once_env.sent == []

    def test_state_is_not_written_for_an_alert_that_was_never_sent(
        self, run_once_env, monkeypatch
    ):
        run_once_env.curr = self.WRONG_SIDE
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        signal_bot.run_once()
        assert signal_bot.load_last_signal() is None

    def test_a_refused_trade_does_not_block_the_next_valid_one(
        self, run_once_env, monkeypatch
    ):
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        run_once_env.curr = self.WRONG_SIDE
        signal_bot.run_once()
        # Price recovers above the stop on the next poll.
        run_once_env.curr = dict(BUY_CURR)
        signal_bot.run_once()
        assert len(run_once_env.sent) == 1

    def test_max_risk_ceiling_also_suppresses_the_alert(self, run_once_env, monkeypatch):
        # Risk is 9.5 against ATR 10, so a 0.5 ATR ceiling rejects it.
        monkeypatch.setattr(signal_bot, "MAX_RISK_ATR", 0.5)
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        signal_bot.run_once()
        assert run_once_env.sent == []
        assert signal_bot.load_last_signal() is None

    def test_a_valid_setup_still_alerts_and_records_state(self, run_once_env, monkeypatch):
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())
        signal_bot.run_once()
        assert len(run_once_env.sent) == 1
        assert signal_bot.load_last_signal()["signal"] == "BUY"

    def test_state_is_not_recorded_when_the_discord_send_fails(
        self, run_once_env, monkeypatch
    ):
        """
        State must be written only after the alert is actually delivered.
        Recording it first would mark a dropped alert as sent, and the next
        poll would treat it as old news instead of retrying.
        """
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())

        def boom(*a, **k):
            raise signal_bot.requests.exceptions.HTTPError("503 from Discord")

        monkeypatch.setattr(signal_bot, "send_discord_alert", boom)

        with pytest.raises(signal_bot.requests.exceptions.HTTPError):
            signal_bot.run_once()
        assert signal_bot.load_last_signal() is None

    def test_the_retry_after_a_failed_send_goes_through(self, run_once_env, monkeypatch):
        monkeypatch.setattr(signal_bot, "detect_signal", detect_stub())

        def boom(*a, **k):
            raise signal_bot.requests.exceptions.HTTPError("503 from Discord")

        monkeypatch.setattr(signal_bot, "send_discord_alert", boom)
        with pytest.raises(signal_bot.requests.exceptions.HTTPError):
            signal_bot.run_once()

        # Webhook recovers on the next poll; the signal is still unsent, so it
        # must fire rather than being suppressed as already-seen.
        monkeypatch.setattr(
            signal_bot, "send_discord_alert",
            lambda *a, **k: run_once_env.sent.append((a, k)),
        )
        signal_bot.run_once()
        assert len(run_once_env.sent) == 1
        assert signal_bot.load_last_signal()["signal"] == "BUY"


# ---------------------------------------------------------------------------
# send_discord_alert
# ---------------------------------------------------------------------------

class _FakeResponse:
    def raise_for_status(self):
        pass  # would raise on a real HTTP error; nothing to do here


ALERT_KWARGS = dict(
    signal="BUY", price=4653.60, atr=4.8, strength=0.8, tier="STRONG", depth=1,
    reason="Green arrow (swing low) + EMA20/50/100 bull stack, pullback to the EMA20",
    ema_fast=4650.0, ema_mid=4640.0, ema_slow=4620.0,
    sl=4638.8, tp=4675.8, rr=1.5,
)


class TestSendDiscordAlert:
    def test_posts_expected_fields_in_content(self, monkeypatch):
        captured = {}

        def fake_post(url, json, timeout):
            captured.update(url=url, json=json, timeout=timeout)
            return _FakeResponse()

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        signal_bot.send_discord_alert(**ALERT_KWARGS)

        content = captured["json"]["content"]
        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "STRONG BUY" in content
        assert ALERT_KWARGS["reason"] in content
        assert "Pullback depth: 1" in content
        assert "4,653.60" in content
        assert "SL: 4,638.80" in content
        assert "TP: 4,675.80" in content
        assert "1:1.50" in content
        assert captured["timeout"] == 10

    def test_depth_one_names_the_mid_ema_as_the_stop_reference(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert(**dict(ALERT_KWARGS, depth=1))
        assert f"stop referenced to EMA{signal_bot.EMA_MID}" in captured["json"]["content"]

    def test_depth_two_names_the_slow_ema_as_the_stop_reference(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert(**dict(ALERT_KWARGS, depth=2))
        assert f"stop referenced to EMA{signal_bot.EMA_SLOW}" in captured["json"]["content"]

    def test_weak_tier_is_labeled(self, monkeypatch):
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
        signal_bot.send_discord_alert(**dict(ALERT_KWARGS, signal="SELL"))
        assert "\U0001F534" in captured["json"]["content"]

    def test_buy_alert_uses_green_emoji(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            signal_bot.requests, "post",
            lambda url, json, timeout: captured.update(json=json) or _FakeResponse(),
        )
        signal_bot.send_discord_alert(**ALERT_KWARGS)
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
            captured.update(url=url, json=json)
            return _FakeResponse()

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        signal_bot.send_startup_message()

        content = captured["json"]["content"]
        assert captured["url"] == signal_bot.DISCORD_WEBHOOK_URL
        assert "Signal bot started" in content
        assert "Williams Fractals" in content
        assert "Veto" in content
        assert "closed candles only" in content

    def test_swallows_request_exceptions_instead_of_raising(self, monkeypatch):
        def fake_post(url, json, timeout):
            raise signal_bot.requests.exceptions.ConnectionError("no network")

        monkeypatch.setattr(signal_bot.requests, "post", fake_post)
        # Should not raise -- startup shouldn't crash the whole bot.
        signal_bot.send_startup_message()
