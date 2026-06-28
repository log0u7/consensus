# User interfaces

Two interfaces are available: a **web UI** served by the FastAPI backend, and a
**terminal UI (TUI)** built with Textual. Both connect to the same API.

## Web UI

The web UI is a single-page app served from `src/static/index.html`. It is
automatically served by the FastAPI backend at the root URL.

Access it at `http://localhost:8800` after `make up`.

### Layout

Three columns:
1. **Spec / Run** - enter the task specification, pick a team, enable RAG, start
   the run.
2. **Reviews** - displays each reviewer's output as it streams in.
3. **Lead / Chat** - the lead's verdict and final code; then a live chat with
   the lead about this session.

### Streaming

The UI uses SSE (`/api/run/stream`) to receive events in real time. The run
button is disabled while a run is in progress. On error the UI never
auto-restarts; the user must re-run manually.

### Export

After a run, the export buttons let you download the generated files as a
ZIP or TAR archive. Archive formats: `zip`, `tar`, `tar.gz`, `tar.bz2`,
`tar.xz`, `7z` (requires `py7zr`).

### Low-quota toggle

The header shows a quota pill. Click it (or `POST /api/quota`) to toggle
low-quota mode. When active, the coder and consensus use a cheaper model and
the panel is reduced. The lead is never downgraded.

### Syntax highlighting

Code blocks use [Highlight.js](https://highlightjs.org/) (BSD-3), vendored
under `src/static/vendor/` - no CDN, works offline.

## Terminal UI (TUI)

The TUI is a Textual application under `tui/`. It connects to the running
backend via the REST API (not directly to the pipeline).

### Requirements

```
pip install textual
```

### Launch

```
python -m tui                         # connects to http://localhost:8800
python -m tui --api-url http://...    # custom backend URL
```

### Key bindings

| Binding   | Action                            |
|-----------|-----------------------------------|
| `Ctrl+R`  | Focus the spec input (Run screen) |
| `Ctrl+C`  | Open the chat screen              |
| `Ctrl+F`  | Open the artifact browser         |
| `Ctrl+O`  | Toggle low-quota mode             |
| `Ctrl+P`  | Show the quota profile            |
| `Ctrl+Q`  | Quit                              |

### Screens

- **Run**: enter a spec, submit, watch streaming output.
- **Chat**: conversational follow-up with the lead after a run.
- **Artifacts**: browse generated files, download archives.
- **Quota**: quota profile modal (active/standby models, panel).

## API endpoints

The backend (`src/api.py`) exposes:

| Method | Path                       | Description                                   |
|--------|----------------------------|-----------------------------------------------|
| POST   | `/api/run`                 | Non-streaming run, returns full result        |
| POST   | `/api/run/stream`          | SSE streaming run                             |
| POST   | `/api/chat`                | Non-streaming chat turn                       |
| POST   | `/api/chat/stream`         | SSE streaming chat turn                       |
| POST   | `/api/chat/regen-artifacts`| Ask the lead to regenerate the file tree      |
| POST   | `/api/archive`             | Pack session files into an archive            |
| GET    | `/api/archive/formats`     | List supported archive formats                |
| DELETE | `/api/session/{id}`        | Delete a session                              |
| GET    | `/api/quota`               | Get the current quota profile                 |
| POST   | `/api/quota`               | Set low-quota mode (`{"low_quota": bool}`)    |
| GET    | `/api/health`              | Health check; `?check_provider=zen` probes gateway |

### SSE event types

Events emitted during `/api/run/stream`:

| type        | Payload fields                                                   |
|-------------|------------------------------------------------------------------|
| `code`      | `code`, `language`, `usage`                                      |
| `review`    | `review` (Review object), `usage`                                |
| `consensus` | `consensus` (ConsensusReport), `usage`                           |
| `result`    | `result` (PipelineResult), `session_id`                          |
| `step`      | `role`, `output`, `usage` (pipeline topology)                    |
| `iteration` | `i`, `role`, `output`, `usage` (loop topology)                   |
| `error`     | `error` (string)                                                 |

Heartbeat comment lines (`: ping`) are sent every few seconds during silent
phases to keep the SSE connection alive through proxies.

### Sessions

A session is created at the end of each successful run and stores the
`PipelineResult`, the lead system prompt, and the conversation history. Sessions
have a TTL (default: 4 hours) and an LRU cap. Set `SESSION_BACKEND=postgres` to
persist sessions across restarts (requires `PG_DSN`).

### Run request schema

```json
{
  "spec":     "string (required)",
  "team":     "string (default: consensus)",
  "use_rag":  false,
  "run_id":   "optional string"
}
```
