"""Pytest setup: ensure src.config can import without a real provider key,
and keep the pricing catalog refresh offline (no network in tests)."""

import os

os.environ.setdefault("ZEN_API_KEY", "dummy")
os.environ.setdefault("PRICING_REFRESH", "0")
