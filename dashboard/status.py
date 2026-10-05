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
WEBHOOK_RE = re.compile(r"https?://[\w.-]*discord(?:app)?\.com/api/webhooks/\S+", re.I)
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


def _age_seconds(ts, now):
    try:
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
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
