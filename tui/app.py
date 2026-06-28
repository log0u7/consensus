"""Main Textual TUI application."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.binding import Binding

from tui.api import APIClient
from tui.screens.artifacts import ArtifactScreen
from tui.screens.chat import ChatScreen
from tui.screens.quota import QuotaScreen
from tui.screens.run import RunDone, RunScreen, RunStarted
from tui.widgets.status_bar import StatusBar

APP_CSS = """
Screen {
    background: $surface;
}

StatusBar {
    dock: top;
    height: 1;
    background: $panel;
    color: $text;
}

.status-bar {
    layout: horizontal;
    align: center middle;
}

.status-title {
    padding: 0 2;
    text-style: bold;
}
.status-connection {
    padding: 0 1;
}
.status-connection.ok {
    color: $success;
}
.status-connection.ng {
    color: $error;
}
.status-quota {
    padding: 0 1;
}
.status-quota.on {
    color: $warning;
    text-style: bold;
}
.status-version {
    padding: 0 1;
    color: $text-disabled;
}

Header {
    background: $primary-background;
}
Footer {
    background: $panel;
}
"""


class TUIApp(App):
    """Consensus TUI application."""

    CSS = APP_CSS
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+r", "focus_run", "Run", show=True),
        Binding("ctrl+c", "open_chat", "Chat", show=True),
        Binding("ctrl+f", "open_files", "Files", show=True),
        Binding("ctrl+o", "toggle_quota", "Toggle quota", show=True),
        Binding("ctrl+p", "show_quota_profile", "Quota profile", show=True),
    ]

    def __init__(self, api_url: str = "http://localhost:8800", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_client = APIClient(base_url=api_url)
        self.current_session_id: str | None = None
        self.current_result: dict[str, Any] | None = None

    async def on_mount(self) -> None:
        self.title = "Consensus"
        self.push_screen(RunScreen(api_client=self._api_client))
        # attempt connection check
        try:
            await self._api_client.health()
            self.query_one(StatusBar).connected = True
        except Exception:
            self.query_one(StatusBar).connected = False
        # fetch initial quota state
        try:
            quota = await self._api_client.get_quota()
            status_bar = self.query_one(StatusBar)
            status_bar.update_from_quota(quota)
        except Exception:
            pass

    async def action_toggle_quota(self) -> None:
        """Toggle low-quota mode."""
        status_bar = self.query_one(StatusBar)
        try:
            quota = await self._api_client.set_quota(not status_bar.low_quota)
            status_bar.update_from_quota(quota)
        except Exception as exc:
            self.notify(f"Failed to toggle quota: {exc}", severity="error")

    async def action_focus_run(self) -> None:
        """Focus the spec input on the run screen."""
        screen = self.screen
        if isinstance(screen, RunScreen):
            spec_area = screen.query_one("#run-spec-area")
            spec_area.focus()
        else:
            self.push_screen(RunScreen(api_client=self._api_client))

    async def on_run_done(self, message: RunDone) -> None:
        """Handle pipeline completion."""
        if message.session_id:
            self.current_session_id = message.session_id
            self.current_result = message.result
            self.notify(
                f"Pipeline complete - session {message.session_id[:8]}... "
                "Press Ctrl+C to chat",
                severity="information",
                timeout=8,
            )
        elif message.error:
            self.notify(f"Pipeline failed: {message.error}", severity="error", timeout=5)

    async def on_run_started(self, _message: RunStarted) -> None:
        self.notify("Pipeline started", severity="information", timeout=2)

    async def action_open_chat(self) -> None:
        """Open the chat screen for the current session."""
        if not self.current_session_id:
            self.notify("No session available - run a pipeline first", severity="warning", timeout=4)
            return
        if isinstance(self.screen, ChatScreen):
            return
        self.push_screen(
            ChatScreen(api_client=self._api_client, session_id=self.current_session_id)
        )

    async def action_open_files(self) -> None:
        """Open the artifact browser for the current result."""
        if not self.current_result or not self.current_session_id:
            self.notify("No result available - run a pipeline first", severity="warning", timeout=4)
            return
        if isinstance(self.screen, ArtifactScreen):
            return
        files = self.current_result.get("files", [])
        if not files:
            # fallback: single-file mode uses code as the only file
            code = self.current_result.get("code", "")
            lang = self.current_result.get("language", "")
            if code:
                files = [{"path": f"output.{lang or 'txt'}", "language": lang, "content": code}]
            else:
                self.notify("No files or code in the result", severity="warning", timeout=4)
                return
        self.push_screen(
            ArtifactScreen(
                api_client=self._api_client,
                files=files,
                session_id=self.current_session_id,
            )
        )

    async def action_show_quota_profile(self) -> None:
        """Show the current quota profile in a modal."""
        try:
            profile = await self._api_client.get_quota()
        except Exception as exc:
            self.notify(f"Failed to fetch quota profile: {exc}", severity="error", timeout=4)
            return
        self.push_screen(QuotaScreen(profile=profile))
