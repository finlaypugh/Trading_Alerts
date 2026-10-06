"""
Read-only views of what the bot is doing: its status and state files, its
effective config and its log. Nothing here raises on a missing or corrupt
file; the dashboard has to keep working while the bot is down.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from outcomes import resolve

ROOT = Path(__file__).resolve().parent.parent
BOT_SERVICE = "signal-bot"

# Health thresholds, in multiples of the bot's poll interval.
STALE_POLLS = 2
DOWN_POLLS = 10

# Only these keys ever leave the status file. Anything else the bot writes in
# future stays invisible until it is added here.
STATUS_FIELDS = (
    "ts_utc", "ticker", "interval", "poll_seconds", "result",
    "last_poll_ok", "last_error", "consecutive_errors",
    "bars_loaded", "last_bar_time", "close", "ema_fast", "ema_mid", "ema_slow",
    "atr", "stack", "long_depth", "short_depth", "long_vetoed", "short_vetoed",
)
STATE_FIELDS = ("signal", "tier", "depth", "bar_time")
BAR_FIELDS = (
    "time", "open", "high", "low", "close", "ema_fast", "ema_mid", "ema_slow",
    "green_arrow", "red_arrow",
)
ALERT_FIELDS = (
    "sent_utc", "bar_time", "signal", "tier", "depth", "strength",
    "price", "sl", "tp", "rr", "risk", "reason",
)

# Effective bot config shown in the UI: response key -> signal_bot attribute.
# An allowlist, so the webhook URL and API token are excluded by construction.
CONFIG_KEYS = {
    "ticker": "TICKER",
    "interval": "INTERVAL",
    "lookback": "LOOKBACK",
    "oanda_environment": "OANDA_ENVIRONMENT",
    "ema_fast": "EMA_FAST",
    "ema_mid": "EMA_MID",
    "ema_slow": "EMA_SLOW",
    "fractal_n": "FRACTAL_N",
    "fractal_max_plateau": "FRACTAL_MAX_PLATEAU",
    "pullback_expiry_bars": "PULLBACK_EXPIRY_BARS",
    "require_pullback": "REQUIRE_PULLBACK",
    "require_pivot_in_pullback": "REQUIRE_PIVOT_IN_PULLBACK",
    "min_stack_bars": "MIN_STACK_BARS",
    "short_max_depth": "SHORT_MAX_DEPTH",
    "rr": "RR",
    "sl_buffer_atr": "SL_BUFFER_ATR",
    "min_stack_sep_atr": "MIN_STACK_SEP_ATR",
    "max_risk_atr": "MAX_RISK_ATR",
    "atr_len": "ATR_LEN",
    "drop_unclosed_bar": "DROP_UNCLOSED_BAR",
    "session_gap_mult": "SESSION_GAP_MULT",
    "cooldown_bars": "COOLDOWN_BARS",
    "weak_strength_cap": "WEAK_STRENGTH_CAP",
    "poll_seconds": "POLL_SECONDS",
}

SECRET_ENV = ("DISCORD_WEBHOOK_URL", "OANDA_API_TOKEN", "DASHBOARD_TOKEN")
# Scheme and host optional: urllib3 connection errors quote only the path,
# "Max retries exceeded with url: /api/webhooks/<id>/<token>".
WEBHOOK_RE = re.compile(
    r"(?:https?://[\w.-]*discord(?:app)?\.com)?/api/webhooks/[\w-]+(?:/[\w-]+)?", re.I
)
REDACTED = "<redacted>"


def redact(text):
    """Remove secret env values and anything shaped like a Discord webhook."""
    text = str(text)
    for name in SECRET_ENV:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, REDACTED)
    return WEBHOOK_RE.sub(REDACTED, text)


def ticker():
    return os.environ.get("SIGNAL_TICKER", "").replace("/", "_")


def status_path():
    return ROOT / f".status_{ticker()}.json"


def state_path():
    return ROOT / f".state_{ticker()}.json"


def bars_path():
    return ROOT / f".bars_{ticker()}.json"


def alerts_path():
    return ROOT / f".alerts_{ticker()}.jsonl"


def _read_json(path):
    """(data, error). A missing file is (None, None), not an error."""
    try:
        return json.loads(Path(path).read_text()), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"{Path(path).name} unreadable: {type(exc).__name__}"


def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _poll_seconds(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    try:
        return float(os.environ.get("SIGNAL_POLL_SECONDS", 30))
    except ValueError:
        return 30.0


def _parse_utc(ts):
    """Aware datetime from an ISO string (naive means UTC), or None."""
    try:
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return then.replace(tzinfo=timezone.utc) if then.tzinfo is None else then


def _epoch(ts):
    """Unix seconds for an ISO string, or None. The chart keys bars on these."""
    then = _parse_utc(ts)
    return None if then is None else int(then.timestamp())


def _age_seconds(ts, now):
    then = _parse_utc(ts)
    if then is None:
        return None
    return max(0.0, (now - then).total_seconds())


def health(age, poll_seconds, last_poll_ok):
    """ok / error / stale / down / no_data."""
    if age is None:
        return "no_data"
    if age > DOWN_POLLS * poll_seconds:
        return "down"
    if age > STALE_POLLS * poll_seconds:
        return "stale"
    if last_poll_ok is False:
        return "error"
    return "ok"


def load_status(now=None):
    now = now or datetime.now(timezone.utc)
    data, error = _read_json(status_path())
    if data is not None and not isinstance(data, dict):
        data, error = None, f"{status_path().name} is not a JSON object"
    if data is None:
        return {
            "health": "no_data",
            "message": error or "no data yet: the bot has not written a status file",
            "age_seconds": None,
            "status": None,
        }

    status = {k: _clean(data.get(k)) for k in STATUS_FIELDS}
    age = _age_seconds(status["ts_utc"], now)
    return {
        "health": health(age, _poll_seconds(status["poll_seconds"]), status["last_poll_ok"]),
        "message": None,
        "age_seconds": None if age is None else round(age, 1),
        "status": status,
    }


def load_last_signal():
    data, error = _read_json(state_path())
    if isinstance(data, str):
        # Bot versions before the dict format stored a bare direction.
        data = {"signal": data}
    if not isinstance(data, dict) or not data.get("signal"):
        if data is not None and error is None:
            error = f"{state_path().name} has no signal"
        return {"last_signal": None, "message": error or "no alert sent yet"}
    return {"last_signal": {k: _clean(data.get(k)) for k in STATE_FIELDS}, "message": None}


def load_bars():
    """The bot's recent bars for the chart, oldest first, each with a unix `ts`."""
    path = bars_path()
    data, error = _read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("bars"), list):
        if data is not None and error is None:
            error = f"{path.name} has no bars"
        return {"bars": [], "message": error or "no bars yet: the bot writes them once warmed up"}
    bars = []
    for raw in data["bars"]:
        if not isinstance(raw, dict):
            continue
        ts = _epoch(raw.get("time"))
        if ts is None:
            continue
        bars.append({**{k: _clean(raw.get(k)) for k in BAR_FIELDS}, "ts": ts})
    return {
        "ticker": _clean(data.get("ticker")),
        "interval": _clean(data.get("interval")),
        "bars": bars,
        "message": None if bars else f"{path.name} has no usable bars",
    }


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def add_outcomes(alerts, bars):
    """
    Mark each alert win / loss / open by walking the chart bars after the bar
    it fired on, with the backtest's own rule: the stop wins when one bar spans
    both levels. "unknown" when that bar is older than the bars file or the
    levels are missing. `r` is in multiples of the alert's risk; `exit_ts` is
    the bar that hit the stop or target.
    """
    usable = [b for b in bars if _number(b.get("high")) and _number(b.get("low"))]
    index = {b["ts"]: i for i, b in enumerate(usable)}
    highs = [b["high"] for b in usable]
    lows = [b["low"] for b in usable]
    for a in alerts:
        a.update(outcome="unknown", r=None, exit_ts=None)
        price, sl, tp = a.get("price"), a.get("sl"), a.get("tp")
        i = index.get(a.get("ts"))
        if (i is None or a.get("signal") not in ("BUY", "SELL")
                or not all(_number(v) for v in (price, sl, tp)) or price == sl):
            continue
        outcome, exit_i = resolve(highs, lows, i, a["signal"], sl, tp)
        a["outcome"] = outcome
        if outcome == "win":
            a["r"] = round(abs(tp - price) / abs(price - sl), 2)
        elif outcome == "loss":
            a["r"] = -1.0
        if exit_i is not None:
            a["exit_ts"] = usable[exit_i]["ts"]
    return alerts


def outcome_summary(alerts):
    """Per side: outcome counts, and win rate and net R over closed trades only."""
    summary = {}
    for side in ("all", "BUY", "SELL"):
        rows = [a for a in alerts if side == "all" or a.get("signal") == side]
        count = {k: sum(1 for a in rows if a.get("outcome") == k)
                 for k in ("win", "loss", "open", "unknown")}
        closed = count["win"] + count["loss"]
        summary[side] = {
            "wins": count["win"], "losses": count["loss"],
            "open": count["open"], "unknown": count["unknown"],
            "win_rate": round(count["win"] / closed, 3) if closed else None,
            "net_r": round(sum(a["r"] for a in rows if a.get("r") is not None), 2),
        }
    return summary


def load_alerts(n):
    """
    The newest n alerts from the bot's alert log, newest first, each with the
    unix `ts` of the bar it fired on and its outcome against the chart bars.
    Unparseable lines are skipped and counted rather than failing the lot.
    """
    path = alerts_path()
    try:
        lines = _tail_file(path, n)
    except FileNotFoundError:
        return {"alerts": [], "skipped": 0, "summary": outcome_summary([]),
                "message": "no alerts logged yet"}
    except OSError as exc:
        return {"alerts": [], "skipped": 0, "summary": outcome_summary([]),
                "message": f"{path.name} unreadable: {type(exc).__name__}"}
    alerts, skipped = [], 0
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            skipped += 1
            continue
        alerts.append({**{k: _clean(raw.get(k)) for k in ALERT_FIELDS},
                       "ts": _epoch(raw.get("bar_time"))})
    add_outcomes(alerts, load_bars()["bars"])
    return {
        "alerts": alerts,
        "skipped": skipped,
        "summary": outcome_summary(alerts),
        "message": None if alerts else "no alerts logged yet",
    }


def bot_config():
    """The bot's effective config, defaults included, via its own module."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import signal_bot
    except Exception as exc:
        return {"config": None, "message": redact(f"cannot load signal_bot: {exc}")}
    return {
        "config": {key: _clean(getattr(signal_bot, attr)) for key, attr in CONFIG_KEYS.items()},
        "message": None,
    }


def _tail_file(path, n, max_bytes=256 * 1024):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably cut in half
    return lines[-n:]


def log_tail(n):
    """
    Last n lines of bot output. DASHBOARD_LOG_FILE wins when set (PM2 logs,
    a redirected run.sh); otherwise the signal-bot unit's journal on Linux.
    """
    log_file = os.environ.get("DASHBOARD_LOG_FILE", "").strip()
    try:
        if log_file:
            lines, source = _tail_file(log_file, n), f"file {Path(log_file).name}"
        elif sys.platform.startswith("linux") and shutil.which("journalctl"):
            proc = subprocess.run(
                ["journalctl", "-u", BOT_SERVICE, "-n", str(n), "--no-pager", "-o", "short-iso"],
                capture_output=True, text=True, timeout=10,
            )
            lines, source = proc.stdout.splitlines()[-n:], f"journalctl -u {BOT_SERVICE}"
            if proc.returncode != 0 and not lines:
                lines = proc.stderr.splitlines()[-n:]
        else:
            return {"source": None, "lines": [],
                    "message": "no log source: set DASHBOARD_LOG_FILE"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"source": None, "lines": [], "message": redact(f"cannot read log: {exc}")}
    return {"source": source, "lines": [redact(line) for line in lines], "message": None}
