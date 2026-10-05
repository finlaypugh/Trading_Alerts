"""python -m dashboard: serve the app with waitress on DASHBOARD_HOST:DASHBOARD_PORT."""
import logging
import os

from waitress import serve

from .app import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
port = int(os.environ.get("DASHBOARD_PORT", 8080))

print(f"Dashboard on http://{host}:{port}", flush=True)
serve(app, host=host, port=port, threads=4)
