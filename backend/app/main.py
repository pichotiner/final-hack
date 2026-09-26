"""Uvicorn entry point for the role 1 server."""

from app.api.server import create_app

app = create_app(shared=True)
