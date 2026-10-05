"""
Whitelisted quick commands. The browser names an action; it never supplies a
command, argument or path. Every subprocess is a fixed argument list, never
shell=True, with a timeout and truncated output.
"""
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from . import status

OUTPUT_LIMIT = 4096

# Must match deploy/sudoers-signal-bot character for character: sudo matches
# on the path as given.
SYSTEMCTL = "/bin/systemctl"

POLL_TIMEOUT = 120
SYSTEMCTL_TIMEOUT = 30
PIP_TIMEOUT = 600


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    confirm: bool
    run: Callable[[], "tuple[bool, str]"]
    linux_only: bool = False

    @property
    def supported(self):
        return not self.linux_only or is_linux()


class Busy(Exception):
    """Another action holds the lock."""


def is_linux():
    return sys.platform.startswith("linux")


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_command(args, timeout):
    """(ok, combined output) for a fixed argument list."""
    try:
        proc = subprocess.run(
            args, cwd=status.ROOT, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"timed out after {timeout}s\n{_text(exc.stdout)}{_text(exc.stderr)}"
    except OSError as exc:
        return False, f"could not run {args[0]}: {exc}"
    output = (_text(proc.stdout) + _text(proc.stderr)).strip()
    return proc.returncode == 0, output or f"exit code {proc.returncode}"


def truncate(text, limit=OUTPUT_LIMIT):
    """Keep the tail: errors and summaries come last."""
    if len(text) <= limit:
        return text
    return "…[truncated]\n" + text[-limit:]


# ---- platform adapter ----

def systemctl(verb):
    """sudo systemctl <verb> signal-bot on Linux; a clean refusal elsewhere."""
    if not is_linux():
        return False, f"not supported on this platform ({sys.platform}): needs systemd"
    # -n: fail instead of prompting if the sudoers rule is missing.
    return run_command(
        ["sudo", "-n", SYSTEMCTL, verb, status.BOT_SERVICE], timeout=SYSTEMCTL_TIMEOUT
    )


# ---- actions ----

def test_alert():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False, "DISCORD_WEBHOOK_URL is not set in the dashboard's environment"
    content = (
        f"\U0001F9EA **Test message from the signal bot dashboard** "
        f"({os.environ.get('SIGNAL_TICKER', '?')}). Not a signal, nothing to act on."
    )
    resp = requests.post(url, json={"content": content}, timeout=10)
    resp.raise_for_status()
    return True, f"posted test message (HTTP {resp.status_code})"


def poll_now():
    # Its own process, so a hang or crash in the poll cannot take the
    # dashboard with it. It can send a real alert, exactly as the bot would.
    return run_command(
        [sys.executable, "-c", "import signal_bot; signal_bot.run_once()"],
        timeout=POLL_TIMEOUT,
    )


def clear_state():
    path = status.state_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return True, f"{path.name} did not exist"
    return True, f"deleted {path.name}: cooldown and last alert reset"


def update_deps():
    ok, output = run_command(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        timeout=PIP_TIMEOUT,
    )
    if ok:
        output += "\n\nRestart the bot and the dashboard to load updated packages."
    return ok, output


ACTIONS = {a.name: a for a in (
    Action("test_alert", "Send test alert", False, test_alert),
    Action("poll_now", "Poll now", False, poll_now),
    Action("start_bot", "Start bot", False, lambda: systemctl("start"), linux_only=True),
    Action("restart_bot", "Restart bot", True, lambda: systemctl("restart"), linux_only=True),
    Action("stop_bot", "Stop bot", True, lambda: systemctl("stop"), linux_only=True),
    Action("clear_state", "Clear last signal", True, clear_state),
    Action("update_deps", "Update dependencies", True, update_deps),
)}

_lock = threading.Lock()


def execute(action):
    """Run one action. Raises Busy if another is in progress."""
    if not _lock.acquire(blocking=False):
        raise Busy(action.name)
    start = time.monotonic()
    try:
        try:
            ok, output = action.run()
        except Exception as exc:
            ok, output = False, f"{type(exc).__name__}: {exc}"
    finally:
        _lock.release()
    return {
        "ok": ok,
        "output": truncate(status.redact(output)),
        "duration_ms": int((time.monotonic() - start) * 1000),
    }
