"""Status bar showing quota mode and connection state."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Label, Static


class StatusBar(Static):
    """Top bar showing API connection status, low-quota indicator and
    the running session's accumulated cost."""

    connected: reactive[bool] = reactive(False)
    low_quota: reactive[bool] = reactive(False)
    session_cost: reactive[float] = reactive(0.0)
    session_cached: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="status-bar"):
            yield Label("Consensus", classes="status-title")
            yield Label("disconnected", classes="status-connection", id="status-connection")
            yield Label("", classes="status-quota", id="status-quota")
            yield Label("", classes="status-cost", id="status-cost")
            yield Label("v0.1.0", classes="status-version")

    def watch_connected(self, value: bool) -> None:
        lbl = self.query_one("#status-connection", Label)
        if value:
            lbl.update("connected")
            lbl.set_classes("status-connection ok")
        else:
            lbl.update("disconnected")
            lbl.set_classes("status-connection ng")

    def watch_low_quota(self, value: bool) -> None:
        lbl = self.query_one("#status-quota", Label)
        if value:
            lbl.update("low quota")
            lbl.set_classes("status-quota on")
        else:
            lbl.update("")
            lbl.set_classes("status-quota")

    def watch_session_cost(self, value: float) -> None:
        self._render_cost()

    def watch_session_cached(self, value: int) -> None:
        self._render_cost()

    def _render_cost(self) -> None:
        lbl = self.query_one("#status-cost", Label)
        parts = []
        if self.session_cost > 0:
            parts.append(f"${self.session_cost:.4f}")
        if self.session_cached > 0:
            parts.append(f"{self.session_cached} cached")
        lbl.update(" · ".join(parts))

    def add_usage(self, usage: dict[str, Any]) -> None:
        """Accumulate one turn's usage (cost_summary or usage dict)."""
        if not usage:
            return
        if usage.get("cost_known"):
            self.session_cost += usage.get("cost", 0.0) or 0.0
        self.session_cached += usage.get("cached_tokens", 0) or 0

    def reset_session(self) -> None:
        self.session_cost = 0.0
        self.session_cached = 0

    def update_from_quota(self, quota: dict[str, Any]) -> None:
        self.low_quota = quota.get("low_quota", False)
