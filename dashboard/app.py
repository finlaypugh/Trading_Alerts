"""
Flask app for the signal_bot dashboard. Reads the bot's files; never
imports its polling loop, so a dashboard crash cannot stop alerts.

    python -m dashboard            # waitress on DASHBOARD_HOST:DASHBOARD_PORT

Reads are open to the LAN. Actions need the X-Token header to match
DASHBOARD_TOKEN, and are refused outright while it is unset.
"""
import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request

from . import actions, status

app = Flask(__name__)
app.json.sort_keys = False
log = logging.getLogger("dashboard")

MAX_LOG_LINES = 500

# Action attempts per client IP, counted before the token check so it also
# slows token guessing.
RATE_LIMIT = 10
RATE_WINDOW = 60.0
_attempts = defaultdict(deque)
_attempts_lock = threading.Lock()

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def token_configured():
    return bool(os.environ.get("DASHBOARD_TOKEN", ""))


def token_ok(given):
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    return bool(expected) and hmac.compare_digest(
        (given or "").encode("utf-8"), expected.encode("utf-8")
    )


def same_origin():
    """Browsers send Origin on every POST; it must name this host. curl sends none."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.host


def rate_limited(ip, now=None):
    now = time.monotonic() if now is None else now
    with _attempts_lock:
        recent = _attempts[ip]
        while recent and now - recent[0] >= RATE_WINDOW:
            recent.popleft()
        if len(recent) >= RATE_LIMIT:
            return True
        recent.append(now)
        return False


def refuse(code, message, name, ip):
    log.warning("action %s from %s refused (%d): %s", name, ip, code, message)
    return jsonify(ok=False, output=message), code


@app.get("/")
def index():
    return render_template(
        "index.html", actions=actions.ACTIONS.values(), actions_enabled=token_configured()
    )


@app.get("/api/status")
def api_status():
    return jsonify(status.load_status())


@app.get("/api/last-signal")
def api_last_signal():
    return jsonify(status.load_last_signal())


@app.get("/api/config")
def api_config():
    return jsonify(status.bot_config())


@app.get("/api/logs")
def api_logs():
    n = request.args.get("n", 100, type=int)
    return jsonify(status.log_tail(max(1, min(n, MAX_LOG_LINES))))


@app.post("/api/action/<name>")
def api_action(name):
    ip = request.remote_addr
    if not same_origin():
        return refuse(403, "cross-origin request refused", name, ip)
    if rate_limited(ip):
        return refuse(429, f"rate limited: {RATE_LIMIT} actions per minute", name, ip)
    if not token_configured():
        return refuse(503, "actions disabled: DASHBOARD_TOKEN is not set", name, ip)
    if not token_ok(request.headers.get("X-Token")):
        return refuse(401, "missing or wrong token", name, ip)

    action = actions.ACTIONS.get(name)
    if action is None:
        return refuse(404, f"unknown action {name!r}", name, ip)
    try:
        result = actions.execute(action)
    except actions.Busy:
        return refuse(409, "another action is still running", name, ip)
    log.info("action %s from %s ok=%s in %dms", name, ip, result["ok"], result["duration_ms"])
    return jsonify(result)


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.after_request
def harden_response(resp):
    resp.headers.update(SECURITY_HEADERS)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    # Backstop behind the per-field redaction: no response body leaves with a
    # secret in it, whichever route produced it. Static files are ours and are
    # streamed, so they are skipped.
    if not resp.direct_passthrough and resp.mimetype in (
        "application/json", "text/html", "text/plain",
    ):
        body = resp.get_data(as_text=True)
        clean = status.redact(body)
        if clean != body:
            resp.set_data(clean)
    return resp
