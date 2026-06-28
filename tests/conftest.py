"""Pytest setup: ensure src.config can import without a real provider key."""

import os

os.environ.setdefault("ZEN_API_KEY", "dummy")
