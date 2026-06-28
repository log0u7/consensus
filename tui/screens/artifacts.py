"""Artifact screen - browse files, preview content, and download archives."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Header, Label, ListItem, ListView, RichLog

from tui.api import APIClient
from tui.widgets.status_bar import StatusBar


class ArtifactScreen(Screen):
    """Browse files from the last run result, preview content, and download archives."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+d", "download_archive", "Download"),
    ]

    DEFAULT_CSS = """
    ArtifactScreen {
        layout: vertical;
    }

    #artifact-session-label {
        height: 1;
        margin: 0 1;
        color: $text-disabled;
    }

    #artifact-layout {
        height: 1fr;
        margin: 0 1;
    }

    #file-list-panel {
        width: 30;
        border: solid $secondary;
        height: 1fr;
    }

    #file-list-title {
        background: $primary-background;
        text-style: bold;
        padding: 0 1;
    }

    #file-list {
        height: 1fr;
    }

    #content-panel {
        width: 1fr;
        border: solid $secondary;
        height: 1fr;
        margin: 0 0 0 1;
    }

    #content-title {
        background: $primary-background;
        text-style: bold;
        padding: 0 1;
    }

    #content-view {
        height: 1fr;
    }

    #artifact-actions {
        height: 3;
        align: center middle;
        margin: 0 1 1 1;
    }

    #format-label {
        margin: 0 1 0 0;
    }

    .action-button {
        margin: 0 1;
    }
    """

    def __init__(self, api_client: APIClient, files: list[dict[str, str]], session_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_client = api_client
        self._files = files
        self._session_id = session_id
        self._selected_format = "zip"
        self._formats = ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "7z"]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar()
        yield Label(f"Session: {self._session_id[:16]}... | Files: {len(self._files)}", id="artifact-session-label")
        with Horizontal(id="artifact-layout"):
            with Vertical(id="file-list-panel"):
                yield Label("Files", id="file-list-title")
                yield ListView(id="file-list")
            with Vertical(id="content-panel"):
                yield Label("Content", id="content-title")
                yield RichLog(id="content-view", highlight=True, max_lines=10000, markup=True)
        with Horizontal(id="artifact-actions"):
            yield Label("Format:", id="format-label")
            # Format selector as simple cycle through formats
            fmt_btn = Button(f" {self._selected_format} ", id="format-cycle-button", variant="default")
            fmt_btn.classes = "action-button"
            yield fmt_btn
            yield Button("Download Archive", id="download-button", variant="primary", classes="action-button")

    def on_mount(self) -> None:
        self._populate_file_list()

    def _populate_file_list(self) -> None:
        file_list = self.query_one("#file-list", ListView)
        file_list.clear()
        for f in self._files:
            path = f.get("path", "?")
            file_list.append(ListItem(Label(path)))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None or idx < 0 or idx >= len(self._files):
            return
        f = self._files[idx]
        path = f.get("path", "?")
        content = f.get("content", "")
        lang = f.get("language", "")
        content_view = self.query_one("#content-view", RichLog)
        content_view.clear()
        content_view.write(f"[bold]{path}[/] ({lang})")
        content_view.write("")
        content_view.write(content)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "format-cycle-button":
            idx = self._formats.index(self._selected_format)
            self._selected_format = self._formats[(idx + 1) % len(self._formats)]
            event.button.label = f" {self._selected_format} "
        elif event.button.id == "download-button":
            await self._download_archive()

    async def _download_archive(self) -> None:
        if not self._files:
            self.notify("No files to archive", severity="warning", timeout=3)
            return
        try:
            data = await self._api_client.download_archive(
                files=[{"path": f["path"], "content": f["content"]} for f in self._files],
                fmt=self._selected_format,
                root="project",
            )
            fname = f"project.{self._selected_format}"
            self.notify(
                f"Archive downloaded: {fname} ({len(data)} bytes)",
                severity="information",
                timeout=5,
            )
        except Exception as exc:
            self.notify(f"Download failed: {exc}", severity="error", timeout=5)
