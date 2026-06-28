"""Quota profile screen - read-only display of current model assignments."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class QuotaScreen(ModalScreen[None]):
    """Modal showing the current quota profile."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    QuotaScreen {
        align: center middle;
    }

    #quota-dialog {
        width: 60;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #quota-title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    .quota-row {
        padding: 0 0 0 1;
    }

    #quota-close {
        width: 14;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, profile: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._profile = profile

    def compose(self) -> ComposeResult:
        with Static(id="quota-dialog"):
            yield Label("Quota Profile", id="quota-title")
            low = self._profile.get("low_quota", False)
            yield Label(f"  Low-quota mode: {'ON' if low else 'off'}")
            yield Label(f"  Coder model:    {self._profile.get('coder_model', '?')}")
            yield Label(f"  Consensus model:{self._profile.get('consensus_model', '?')}")
            yield Label(f"  Lead model:     {self._profile.get('lead_model', '?')}")
            yield Label(f"  Panel:          {', '.join(self._profile.get('panel', []))}")
            yield Label("")
            yield Label("  (low-quota profile)")
            yield Label(f"  Low-quota model:{self._profile.get('low_quota_model', '?')}")
            yield Label(f"  Low-quota panel:{', '.join(self._profile.get('low_quota_panel', []))}")
            yield Button("Close", id="quota-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quota-close":
            self.dismiss()
