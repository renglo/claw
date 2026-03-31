# CLAW extension

CLAW is a **ReAct-style agent runtime** for Renglo: it assembles context from sessions, beliefs, journal, and tools; calls an LLM; executes tools via the scheduler; persists structured **session events**; and can stream status and roll-shaped messages to connected clients over WebSockets.

The implementation lives under `package/` as the installable Python module **`claw-mod`**.

## What it provides

| Piece | Role |
|--------|------|
| **`Context`** | Builds `ContextBundle`: system layers (identity, policy, **current date/time**), beliefs, journal, session transcript, tools. Skips UI-only event types (`claw_stream`, `claw_signal`, `claw_subagent_message`) from the model prompt. |
| **`Loop`** | Main react loop: create turn if needed, save inbound `user_message`, iterate LLM → `interpret_model_output` → tool execution → `persist_side_effects`, optional subagents, `print_chat` for stream rows, `on_roll_event` for live roll mirrors. |
| **`Sessions`** | Binds `entity_type`, `entity_id`, `thread` into a session id; `create_turn` / `append_event` / `get_events`; maps `SessionEvent` ↔ turn row shape (`_type`, `_out`, `_meta`). |
| **`Gateway`** | Routes inbound `user_message` (and internal signals) into `Loop.run_turn`. |
| **`Tools`** | Loads `ToolDefinition` from **`schd_tools`** (via `DataController`); supports **array** `input` (`[{name, hint, required}, …]`) and JSON Schema **object** `input`. |
| **`Models`** | OpenAI chat-completions adapter: `ContextBundle` → `choices` + tool calls. |
| **`GenericAgent`** | Scheduler-facing entry: loads config, builds `Sessions`, `Loop`, `Gateway`; wires WebSocket (`on_stream`, `on_roll_event`, subagent `on_message` / `on_signal`) using `connectionId` from the payload. |
| **`SubAgents`** | Parent/worker session bindings, messages, and typed signals (progress, completion, failure, …). |

Higher-level design notes and behavior contracts are in **`docs/claw_specs.md`**. Implementation details and data rings are summarized in **`docs/claw_implementation.md`**.

## Install

From `extensions/claw/package/`:

```bash
pip install -e .
```

Python **3.12+**. Dependencies include **`openai`**; integration with sessions, scheduler, and data layers expects **`renglo`** (for example `pip install -e dev/renglo-lib` from the repo root).

## Configuration

- **`OPENAI_API_KEY`** — required for `Models` / `Loop` LLM calls.
- **`WEBSOCKET_CONNECTIONS`** — API Gateway management (or local dev) URL for `WebSocketClient`; used by `GenericAgent` when `connectionId` is present on the inbound payload.

## Session events (turn ledger)

Roll-style events are stored with `_type`, `_out` (`role`, `content`), and `_meta`. Core types include:

- `user_message`, `assistant_message`
- `tool_call`, `tool_result`
- Realtime mirrors: `claw_stream`, `claw_signal`, `claw_subagent_message` (persisted for reload parity where applicable; stream iteration is persisted in `Loop.print_chat`).

Tool results persist the handler return value’s **`output`** field, not the full `{success, output, stack, …}` envelope.

## Tools (`schd_tools`)

Tools are registered in the **`schd_tools`** ring with at least **`key`**, **`goal`** / **`instructions`**, **`input`** (JSON Schema object **or** PES-style **array** of parameters), and **`handler`** (extension routing for `SchdController.handler_call`).

The **`Tools`** class in `handlers/tools.py` normalizes `input` into OpenAI function `parameters` so the model receives valid argument schemas.

## Blueprints

JSON blueprints under **`blueprints/`** are used with the installer / data layer (e.g. `claw_sessions`, `claw_beliefs`, `claw_journal`, `claw_compaction`). See **`installer/upload_blueprints.py`** for upload flow.

## Related code

- **Renglo** (`dev/renglo-lib`): `SessionController`, `ChatController`, `SchdController`, `DataController`.
- **UI** (e.g. triage): consumes session turns and WebSocket roll payloads; **`on_roll_event`** sends the same shape as stored rows for reload parity.

## License

This extension is licensed under the **Server Side Public License v1** (SSPL-1.0). See **`LICENSE.txt`** in this directory (same terms as the PES extension).

## Package layout

```
extensions/claw/
├── LICENSE.txt               # SSPL-1.0
├── README.md                 # this file
├── docs/
│   ├── claw_specs.md         # design spec
│   └── claw_implementation.md
├── blueprints/               # ring blueprints
├── installer/
└── package/
    ├── pyproject.toml        # claw-mod
    ├── requirements.txt
    └── claw/
        └── handlers/         # Context, Loop, Sessions, Tools, models, gateway, …
```
