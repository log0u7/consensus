"""Status bar showing quota mode and connection state."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Label, Static


class StatusBar(Static):
    """Top bar showing API connection status and low-quota indicator."""

    connected: reactive[bool] = reactive(False)
    low_quota: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="status-bar"):
            yield Label("Consensus", classes="status-title")
            yield Label("disconnected", classes="status-connection", id="status-connection")
            yield Label("", classes="status-quota", id="status-quota")
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

    def update_from_quota(self, quota: dict[str, Any]) -> None:
        self.low_quota = quota.get("low_quota", False)
