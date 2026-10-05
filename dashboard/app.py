"""
Flask app for the signal_bot dashboard. Reads the bot's files; never
imports its polling loop, so a dashboard crash cannot stop alerts.

    python -m dashboard            # waitress on DASHBOARD_HOST:DASHBOARD_PORT
"""
from flask import Flask, jsonify, render_template, request

from . import status

app = Flask(__name__)
app.json.sort_keys = False

MAX_LOG_LINES = 500


@app.get("/")
def index():
    return render_template("index.html")


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


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.after_request
def redact_response(resp):
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
