"""Chat screen - conversation with the Lead model."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Header, Label, RichLog, TextArea

from tui.api import APIClient
from tui.widgets.status_bar import StatusBar


class ChatMessage(Message):
    """A delta chunk arrived in the chat stream."""

    def __init__(self, delta: str) -> None:
        super().__init__()
        self.delta = delta


class ChatDone(Message):
    """Chat stream completed."""

    def __init__(self, usage: dict[str, Any] | None, error: str | None) -> None:
        super().__init__()
        self.usage = usage
        self.error = error


class RegenDone(Message):
    """Artifact regeneration completed."""

    def __init__(self, files: list[dict[str, str]], usage: dict[str, Any] | None, error: str | None) -> None:
        super().__init__()
        self.files = files
        self.usage = usage
        self.error = error


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
        log.write(f"\n[bold]You:[/] {text}")
        input_widget.text = ""
        input_widget.focus()

        log.write("[dim]Lead is thinking...[/]")
        buffer = ""
        try:
            async for event in self._api_client.chat_stream(self._session_id, text):
                if "delta" in event:
                    buffer += event["delta"]
                    # rewrite the last line to show partial response
                    log.write(f"\n[bold]Lead:[/] {buffer}", width=9999)
                elif "done" in event:
                    usage = event.get("usage", {})
                    if usage:
                        log.write(
                            f"\n[dim]--- cost: ${usage.get('cost', 0):.4f} "
                            f"({usage.get('input_tokens', 0)} in / "
                            f"{usage.get('output_tokens', 0)} out)[/]"
                        )
                    self.post_message(ChatDone(usage, None))
                elif "error" in event:
                    log.write(f"\n[bold red]Error: {event['error']}[/]")
                    self.post_message(ChatDone(None, event["error"]))
        except Exception as exc:
            log.write(f"\n[bold red]Connection error: {exc}[/]")
            self.post_message(ChatDone(None, str(exc)))

    async def regen_artifacts(self) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write("\n[dim]Regenerating files...[/]")
        try:
            result = await self._api_client.regen_artifacts(self._session_id)
            files = result.get("files", [])
            usage = result.get("usage", {})
            log.write(f"[bold]Files regenerated: {len(files)} files[/]")
            for f in files:
                log.write(f"  {f.get('path', '?')}")
            if usage:
                log.write(
                    f"[dim]  cost: ${usage.get('cost', 0):.4f} "
                    f"({usage.get('input_tokens', 0)} in / "
                    f"{usage.get('output_tokens', 0)} out)[/]"
                )
            self.post_message(RegenDone(files, usage, None))
        except Exception as exc:
            log.write(f"\n[bold red]Regeneration failed: {exc}[/]")
            self.post_message(RegenDone([], None, str(exc)))
