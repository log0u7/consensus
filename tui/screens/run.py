"""Pipeline run screen - spec input and streaming stage output."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Header, RichLog, TextArea

from tui.api import APIClient
from tui.widgets.status_bar import StatusBar


class RunStarted(Message):
    """Emitted when a run starts (no data)."""


class RunEvent(Message):
    """A new SSE event arrived from the pipeline."""

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__()
        self.event = event


class RunDone(Message):
    """Pipeline completed (result or error)."""

    def __init__(self, session_id: str | None, result: dict[str, Any] | None, error: str | None) -> None:
        super().__init__()
        self.session_id = session_id
        self.result = result
        self.error = error


class RunScreen(Screen):
    """Main screen: spec input, run button, streaming pipeline log."""

    BINDINGS = [
        Binding("ctrl+r", "run_pipeline", "Run"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    RunScreen {
        layout: vertical;
    }

    #run-spec-area {
        height: 8;
        border: solid $primary;
        margin: 1 1 0 1;
    }

    #run-button-row {
        height: 3;
        align: center middle;
        margin: 0 1;
    }

    #run-output {
        height: 1fr;
        border: solid $secondary;
        margin: 0 1 1 1;
    }

    #run-button {
        width: 20;
    }

    #run-button.running {
        background: $warning;
    }

    .event-code {
        color: $text;
    }
    .event-review {
        color: $accent;
    }
    .event-consensus {
        color: $success;
    }
    .event-result {
        color: $primary;
        text-style: bold;
    }
    .event-error {
        color: $error;
        text-style: bold;
    }
    .event-ping {
        color: $text-disabled;
    }
    """

    def __init__(self, api_client: APIClient, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_client = api_client
        self._session_id: str | None = None
        self._result: dict[str, Any] | None = None
        self._running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar()
        yield TextArea(id="run-spec-area", text="", placeholder="Enter your specification here...")
        with Horizontal(id="run-button-row"):
            yield Button("Run Pipeline", id="run-button", variant="primary")
        yield RichLog(id="run-output", highlight=True, max_lines=10000, markup=True)

    def on_mount(self) -> None:
        spec_area = self.query_one("#run-spec-area", TextArea)
        spec_area.focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-button":
            await self.run_pipeline()

    async def run_pipeline(self) -> None:
        if self._running:
            return
        spec_area = self.query_one("#run-spec-area", TextArea)
        spec = spec_area.text.strip()
        if not spec:
            self.query_one("#run-output", RichLog).write("[bold red]Please enter a spec[/]")
            return

        self._running = True
        self._session_id = None
        self._result = None

        btn = self.query_one("#run-button", Button)
        btn.label = "Running..."
        btn.add_class("running")

        output = self.query_one("#run-output", RichLog)
        output.clear()

        self.post_message(RunStarted())
        output.write("[bold cyan]Starting pipeline...[/]\n")

        api = self._api_client

        try:
            async for event in api.run_stream(spec, use_rag=False):
                self.post_message(RunEvent(event))
                self._handle_event(output, event)

                if event.get("type") == "result":
                    self._session_id = event.get("session_id")
                    self._result = event.get("result")
                    self.post_message(RunDone(self._session_id, self._result, None))

                elif event.get("type") == "error":
                    err_msg = event.get("error", "Unknown error")
                    output.write(f"\n[bold red]Error: {err_msg}[/]\n")
                    self.post_message(RunDone(None, None, err_msg))

        except Exception as exc:
            output.write(f"\n[bold red]Connection error: {exc}[/]\n")
            self.post_message(RunDone(None, None, str(exc)))
        finally:
            self._running = False
            btn.label = "Run Pipeline"
            btn.remove_class("running")

    def _handle_event(self, output: RichLog, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype == "code":
            code = event.get("code", "")
            lang = event.get("language", "")
            lines = code.split("\n")
            output.write(f"\n[bold]Code generated[/] ({lang}, {len(lines)} lines)")
            usage = event.get("usage", {})
            if usage:
                output.write(f"  usage: {usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out")

        elif etype == "review":
            review = event.get("review", {})
            name = review.get("reviewer", "?")
            ok = review.get("ok", False)
            issues = review.get("issues", [])
            status = "ok" if ok else "FAILED"
            color = "green" if ok else "red"
            output.write(f"\n[bold {color}]Review: {name} ({status})[/]")
            if review.get("error"):
                output.write(f"  error: {review['error']}")
            for issue in issues:
                sev = issue.get("severity", "?")
                cat = issue.get("category", "?")
                title = issue.get("title", "?")
                output.write(f"  [{_sev_color(sev)}]{sev.upper():12s}[/] [{cat}]{title}[/]")
            if review.get("overall"):
                output.write(f"  overall: {review['overall']}")

        elif etype == "consensus":
            consensus = event.get("consensus", {})
            issues = consensus.get("issues", [])
            output.write(f"\n[bold]Consensus[/] ({len(issues)} issues)")
            for issue in issues:
                sev = issue.get("severity", "?")
                score = issue.get("consensus_score", 0)
                flagged = issue.get("flagged_by", [])
                title = issue.get("title", "?")
                bar = _score_bar(score)
                output.write(
                    f"  [{_sev_color(sev)}]{sev.upper():12s}[/] "
                    f"{bar} {score:.2f} "
                    f"[dim]{', '.join(flagged)}[/] "
                    f"{title}"
                )

        elif etype == "result":
            result = event.get("result", {})
            verdict = result.get("verdict", "?")
            vcolor = {"APPROVE": "green", "APPROVE_WITH_CHANGES": "yellow", "REJECT": "red"}.get(verdict, "white")
            output.write(f"\n[bold {vcolor}]Verdict: {verdict}[/]")
            rationale = result.get("rationale", "")
            if rationale:
                output.write(f"\n{rationale}")
            final_code = result.get("final_code", "")
            if final_code:
                flines = final_code.split("\n")
                output.write(f"\nFinal code ({len(flines)} lines):")
                # show first/last few lines
                for line in flines[:8]:
                    output.write(f"  {line}")
                if len(flines) > 10:
                    output.write(f"  ... ({len(flines) - 10} more lines)")
                    for line in flines[-2:]:
                        output.write(f"  {line}")
            files = result.get("files", [])
            if files:
                output.write(f"\n[bold]Files: {len(files)}[/]")
                for f in files:
                    output.write(f"  {f.get('path', '?')}")
            rag = result.get("rag_sources", [])
            if rag:
                output.write(f"\nRAG sources: {len(rag)} chunks")
            usages = result.get("usages", [])
            for u in usages:
                output.write(
                    f"  [{u.get('step', '?')}] {u.get('model', '?')}: "
                    f"{u.get('input_tokens', 0)} in / {u.get('output_tokens', 0)} out"
                    f" ({u.get('latency_ms', 0)}ms)"
                )
            cost = result.get("cost_summary", {})
            output.write(
                f"\n[bold]Cost: ${cost.get('cost', 0):.4f} "
                f"({cost.get('calls', 0)} calls, "
                f"{cost.get('input_tokens', 0)} in / {cost.get('output_tokens', 0)} out)[/]"
            )

        elif etype == "error":
            output.write(f"\n[bold red]Pipeline error: {event.get('error', 'Unknown')}[/]")

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def result(self) -> dict[str, Any] | None:
        return self._result


def _sev_color(sev: str) -> str:
    return {"critical": "red", "high": "orange", "medium": "yellow", "low": "green"}.get(sev.lower(), "white")


def _score_bar(score: float) -> str:
    n = max(1, round(score * 10))
    return "█" * n + "░" * (10 - n)
