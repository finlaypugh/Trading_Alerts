"""
Unit tests for backtest.py's OANDA fetch layer.

Run locally:
    pip install pytest pandas numpy
    pytest test_backtest.py -v

No network calls happen: requests.get is replaced by a fake candles endpoint
that enforces OANDA's own parameter rules.
"""
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

# Bind signal_bot's config from the test env first. backtest rewrites
# SIGNAL_TICKER / SIGNAL_INTERVAL and parses sys.argv at import, so both are
# put back afterwards rather than leaking into test_signal_bot.
import signal_bot  # noqa: F401

_saved_env, _saved_argv = dict(os.environ), sys.argv
sys.argv = ["backtest.py"]
try:
    import backtest
finally:
    sys.argv = _saved_argv
    os.environ.clear()
    os.environ.update(_saved_env)


# ---------------------------------------------------------------------------
# Fake OANDA
# ---------------------------------------------------------------------------

T0 = pd.Timestamp("2026-09-01 00:00", tz="UTC")


class FakeOanda:
    """
    Serves `n` M1 candles starting at T0. The last one is the in-progress
    candle (complete=false). With overlap=True each page re-sends the candle
    before `from`, the way adjacent pages can share a boundary candle.
    """

    def __init__(self, n, overlap=False):
        self.times = pd.date_range(T0, periods=n, freq="1min")
        self.overlap = overlap
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(dict(params))
        if "count" in params and "from" in params and "to" in params:
            return FakeResponse(400, {"errorMessage": "count with from and to"})

        start = pd.Timestamp(params["from"])
        i = int(self.times.searchsorted(start))
        if self.overlap and i > 0:
            i -= 1
        page = self.times[i: i + int(params["count"])]
        last = len(self.times) - 1
        candles = [
            {
                "time": t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                "complete": (i + k) != last,
                "volume": 10,
                "mid": {"o": "1.0", "h": "2.0", "l": "0.5", "c": f"{1 + (i + k) / 1000}"},
            }
            for k, t in enumerate(page)
        ]
        return FakeResponse(200, {"candles": candles})


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise backtest.requests.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture
def oanda(monkeypatch):
    """Install a FakeOanda; call the returned factory with (n, overlap)."""
    def install(n, overlap=False, page_size=None):
        fake = FakeOanda(n, overlap)
        monkeypatch.setattr(backtest.requests, "get", fake.get)
        if page_size is not None:
            monkeypatch.setattr(backtest, "PAGE_SIZE", page_size)
        return fake
    return install


def fetch(end, days=1):
    return backtest.fetch_history("XAU_USD", "M1", days, "practice", token="t", end=end)


# ---------------------------------------------------------------------------
# fetch_history
# ---------------------------------------------------------------------------

class TestFetchHistory:
    def test_never_sends_count_with_both_from_and_to(self, oanda):
        fake = oanda(200, page_size=30)
        fetch(end=T0 + pd.Timedelta(minutes=150), days=1)
        assert len(fake.calls) > 1
        for params in fake.calls:
            assert "to" not in params
            assert params["count"] == 30

    def test_window_inside_one_page_is_one_call(self, oanda):
        fake = oanda(500)
        df = fetch(end=T0 + pd.Timedelta(minutes=100))
        assert len(fake.calls) == 1
        assert len(df) == 101  # T0 .. T0+100 inclusive

    def test_multi_page_window_paginates_dedups_and_sorts(self, oanda):
        fake = oanda(200, overlap=True, page_size=30)
        end = T0 + pd.Timedelta(minutes=150)
        df = fetch(end=end)
        assert len(fake.calls) >= 6
        assert df.index.is_unique
        assert df.index.is_monotonic_increasing
        assert list(df.index) == list(pd.date_range(T0, end, freq="1min"))

    def test_nothing_past_the_requested_end(self, oanda):
        oanda(500, page_size=40)
        end = T0 + pd.Timedelta(minutes=77, seconds=30)
        df = fetch(end=end)
        assert df.index.max() <= end
        assert df.index.max() == T0 + pd.Timedelta(minutes=77)

    def test_in_progress_candle_is_dropped(self, oanda):
        oanda(50)
        df = fetch(end=T0 + pd.Timedelta(hours=2))
        assert len(df) == 49
        assert df.index.max() == T0 + pd.Timedelta(minutes=48)

    def test_empty_window_returns_empty_frame(self, oanda):
        oanda(0)
        df = fetch(end=T0 + pd.Timedelta(hours=1))
        assert isinstance(df, pd.DataFrame) and df.empty

    def test_output_is_what_compute_indicators_expects(self, oanda):
        oanda(20)
        df = fetch(end=T0 + pd.Timedelta(hours=1))
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert str(df.index.tz) == "UTC"
        assert df["Close"].dtype == float

    @pytest.mark.parametrize("end", [
        pd.Timestamp("2026-09-01 01:40", tz="Europe/London"),   # aware, not UTC
        pd.Timestamp("2026-09-01 00:40", tz="UTC"),
        pd.Timestamp("2026-09-01 00:40"),                        # naive -> UTC
        "2026-09-01T00:40:00Z",
    ])
    def test_aware_or_naive_end_does_not_raise(self, oanda, end):
        oanda(100)
        df = fetch(end=end)
        assert df.index.max() == T0 + pd.Timedelta(minutes=40)

    def test_http_error_raises(self, monkeypatch):
        monkeypatch.setattr(
            backtest.requests, "get",
            lambda *a, **k: FakeResponse(401, {"errorMessage": "bad token"}),
        )
        with pytest.raises(backtest.requests.HTTPError):
            fetch(end=T0)


class TestToUtc:
    def test_aware_timestamp_is_converted_not_rejected(self):
        ts = backtest._to_utc(pd.Timestamp("2026-09-01 12:00", tz="America/New_York"))
        assert ts == pd.Timestamp("2026-09-01 16:00", tz="UTC")

    def test_naive_timestamp_is_read_as_utc(self):
        ts = backtest._to_utc(pd.Timestamp("2026-09-01 12:00"))
        assert ts == pd.Timestamp("2026-09-01 12:00", tz="UTC")


# ---------------------------------------------------------------------------
# Granularity default
# ---------------------------------------------------------------------------

class TestGranularity:
    def test_every_granularity_maps_to_a_parseable_interval(self):
        for gran, interval in backtest.GRANULARITY_TO_INTERVAL.items():
            assert signal_bot.interval_minutes(interval) > 0, gran

    def test_default_is_one_minute_even_with_a_15m_env(self):
        # The requirement this backtest exists for: .env's SIGNAL_INTERVAL is
        # the live bot's polling bar size and must not leak into the default.
        # A subprocess, because the override happens at import time.
        env = dict(os.environ, SIGNAL_INTERVAL="15m", SIGNAL_TICKER="GC=F")
        code = (
            "import sys; sys.argv = ['backtest.py']; "
            "import backtest, signal_bot; "
            "print(signal_bot.INTERVAL, signal_bot.TICKER, backtest.ARGS.granularity)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True,
            cwd=Path(__file__).parent, check=True,
        ).stdout.split()
        assert out == ["1m", "XAU_USD", "M1"]
