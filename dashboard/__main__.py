"""python -m dashboard: serve the app with waitress on DASHBOARD_HOST:DASHBOARD_PORT."""
import os

from waitress import serve

from .app import app

host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
port = int(os.environ.get("DASHBOARD_PORT", 8080))

print(f"Dashboard on http://{host}:{port}")
serve(app, host=host, port=port, threads=4)
