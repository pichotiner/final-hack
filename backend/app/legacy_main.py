"""Explicit P3 draft compatibility entry point for scripts/demo_server.py only."""

from app.api.server import create_app

app = create_app(shared=False)
