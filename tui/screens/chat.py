"""Chat screen - conversation with the Lead model."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Header, Label, RichLog, Static, TextArea

from tui import esc
from tui.api import APIClient
from tui.widgets.status_bar import StatusBar


class ChatScreen(Screen):
    """Conversation with the Lead. Needs a session_id from a completed run."""

    BINDINGS = [
        Binding("ctrl+s", "send_message", "Send"),
        Binding("ctrl+r", "regen_artifacts", "Regen files"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    ChatScreen {
        layout: vertical;
    }

    #chat-session-label {
        height: 1;
        margin: 0 1;
        color: $text-disabled;
    }

    #chat-log {
        height: 1fr;
        border: solid $secondary;
        margin: 0 1;
    }

    #chat-stream {
        display: none;
        height: 3;
        border: solid $accent;
        margin: 0 1;
    }

    #chat-input-row {
        height: 4;
        margin: 0 1 1 1;
    }

    #chat-input {
        height: 3;
        border: solid $primary;
        width: 1fr;
    }

    #chat-send-button {
        width: 12;
        height: 3;
    }

    #chat-actions {
        height: 3;
        align: center middle;
        margin: 0 1;
    }

    #regen-button {
        width: 20;
    }

    .user-msg {
        color: $text;
        text-style: bold;
    }
    .assistant-msg {
        color: $accent;
    }
    .usage-line {
        color: $text-disabled;
    }
    .error-line {
        color: $error;
        text-style: bold;
    }
    """

    def __init__(self, api_client: APIClient, session_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_client = api_client
        self._session_id = session_id
        self._message_count = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar()
        yield Label(f"Session: {self._session_id[:16]}...", id="chat-session-label")
        yield RichLog(id="chat-log", highlight=True, max_lines=10000, markup=True)
        yield Static("", id="chat-stream")
        with Horizontal(id="chat-input-row"):
            yield TextArea(id="chat-input", text="", placeholder="Type your message...")
            yield Button("Send", id="chat-send-button", variant="primary")
        with Horizontal(id="chat-actions"):
            yield Button("Regenerate Files", id="regen-button", variant="default")

    def on_mount(self) -> None:
        self.query_one("#chat-input", TextArea).focus()
        log = self.query_one("#chat-log", RichLog)
        log.write("[bold]Chat with the Lead[/]\n")
        log.write("Type a message and press Ctrl+S or Send to start.\n")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "chat-send-button":
            await self.send_message()
        elif event.button.id == "regen-button":
            await self.regen_artifacts()

    async def send_message(self) -> None:
        input_widget = self.query_one("#chat-input", TextArea)
        text = input_widget.text.strip()
        if not text:
            return

        log = self.query_one("#chat-log", RichLog)
        log.write(f"\n[bold]You:[/] {esc(text)}")
        input_widget.text = ""
        input_widget.focus()

        log.write("[dim]Lead is thinking...[/]")
        stream = self.query_one("#chat-stream", Static)
        buffer = ""
        stream.update("[bold]Lead:[/] ")
        stream.display = True
        try:
            async for event in self._api_client.chat_stream(self._session_id, text):
                if "delta" in event:
                    buffer += event["delta"]
                    stream.update(f"[bold]Lead:[/] {esc(buffer)}")
                elif "done" in event:
                    usage = event.get("usage", {})
                    if buffer:
                        log.write(f"\n[bold]Lead:[/] {esc(buffer)}")
                    if usage:
                        log.write(f"\n[dim]{_usage_line(usage)}[/]")
                        self.query_one(StatusBar).add_usage(usage)
                elif "error" in event:
                    log.write(f"\n[bold red]Error: {esc(event['error'])}[/]")
        except Exception as exc:
            log.write(f"\n[bold red]Connection error: {esc(str(exc))}[/]")
        finally:
            stream.update("")
            stream.display = False

    async def regen_artifacts(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write("\n[dim]Regenerating files...[/]")
        try:
            result = await self._api_client.regen_artifacts(self._session_id)
            files = result.get("files", [])
            usage = result.get("usage", {})
            log.write(f"[bold]Files regenerated: {len(files)} files[/]")
            for f in files:
                log.write(f"  {esc(f.get('path', '?'))}")
            if usage:
                log.write(f"[dim]  {_usage_line(usage)}[/]")
                self.query_one(StatusBar).add_usage(usage)
        except Exception as exc:
            log.write(f"\n[bold red]Regeneration failed: {esc(str(exc))}[/]")


def _usage_line(usage: dict[str, Any]) -> str:
    cost_known = usage.get("cost_known")
    cost = f"${usage.get('cost', 0):.4f}" if cost_known else "cost n/a"
    cached = usage.get("cached_tokens", 0)
    cached_txt = f", {cached} cached" if cached else ""
    return (
        f"cost: {cost} ({usage.get('input_tokens', 0)} in / "
        f"{usage.get('output_tokens', 0)} out{cached_txt})"
    )
