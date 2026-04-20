"""
Triage **workstreams**: separate things the agent is doing (quotes, bookings, …), each with a
stable ``reference_id``, handler/tool name, and status.

This module is the **single integration point**:

1. **Persistence** — ``WorkstreamRegistry`` loads and saves workstreams on the workspace as the
   top-level field ``workstreams``: a **flat** map ``{ reference_id: { ... entry ... }, ... }``. It
   implements :class:`TaskStateStore` only so :class:`Loop` can load state each turn and run
   :meth:`after_tool_calls` after tools.

2. **Prompt** — :class:`Workstreams` subclasses :class:`Context` and injects that dictionary (plus
   derived pending hints) into the model prompt so each turn can resolve which workstream applies.
"""

from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from typing import Any, Optional

from .class_prototypes import (
    ContextBundle,
    IncomingEvent,
    JournalEntry,
    MemoryFact,
    PromptMessage,
    SessionEvent,
    TaskState,
    TaskStateStore,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from .context import Context

_logger = logging.getLogger(__name__)

# Top-level workspace field (see SessionController.update_workspace ``workstreams`` key)
WORKSPACE_WORKSTREAMS_KEY = "workstreams"


def ensure_reference_id() -> str:
    """Opaque id for a new workstream (UUID). Pass as ``reference_id`` to the handler/tool."""
    return str(uuid.uuid4())


def forced_tool_calls_for_pending_workstream_reply(
    task_state: Optional[TaskState],
    incoming_event: IncomingEvent,
) -> list[ToolCall]:
    """
    When exactly one workstream is ``waiting_for_user``, the model must not answer directly;
    route this user turn to that handler (e.g. ``agent_quotes``) with ``reference_id`` + ``message``.

    Used by :class:`Loop` on iteration 1 only so follow-up iterations can summarize after tools run.
    """
    if incoming_event.event_type != "user_message":
        return []
    if not task_state or not task_state.references:
        return []
    aw = task_state.references.get("active_workstreams")
    if not isinstance(aw, dict):
        return []
    waiting: list[tuple[str, dict[str, Any]]] = []
    for rid, entry in aw.items():
        if isinstance(entry, dict) and entry.get("status") == "waiting_for_user":
            waiting.append((str(rid), entry))
    if len(waiting) != 1:
        return []
    ref_id, entry = waiting[0]
    tool = (entry.get("tool") or entry.get("handler") or "").strip()
    if not tool:
        return []
    payload = incoming_event.payload or {}
    user_text = str(payload.get("text") or payload.get("message") or "")
    return [
        ToolCall(
            tool_name=tool,
            arguments={"reference_id": ref_id, "message": user_text},
            call_id=f"forced-workstream-{uuid.uuid4()}",
        )
    ]


def _is_workstream_entry(d: Any) -> bool:
    """True if ``d`` looks like a persisted workstream row (not a stray envelope or nested map)."""
    if not isinstance(d, dict):
        return False
    if d.get("reference_id") is not None:
        return True
    return "tool" in d and "status" in d


def _persisted_workstreams_to_items_map(raw: Any) -> dict[str, Any]:
    """Workspace ``workstreams`` value → map ``reference_id → entry`` (flat dict of dicts)."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): v
        for k, v in raw.items()
        if isinstance(v, dict) and _is_workstream_entry(v)
    }


def _tool_result_requests_workstream_release(val: Any) -> bool:
    """
    True when handler output asks triage to close this workstream (e.g. plan fully done).

    Accepts dict/list or a JSON string encoding of either (some adapters pass stringified output).
    """
    if isinstance(val, dict):
        if val.get('release_workstream') or val.get('plan_execution_complete'):
            return True
        ws = val.get('workstream_status')
        if isinstance(ws, str) and ws.strip().lower() == 'completed':
            return True
        return any(_tool_result_requests_workstream_release(v) for v in val.values())
    if isinstance(val, list):
        return any(_tool_result_requests_workstream_release(x) for x in val)
    if isinstance(val, str):
        s = val.strip()
        if len(s) >= 2 and s[0] in '{[' and s[-1] in '}]':
            try:
                return _tool_result_requests_workstream_release(json.loads(s))
            except Exception:
                pass
    return False


def _preview(val: Any, max_len: int) -> str:
    try:
        s = val if isinstance(val, str) else json.dumps(val, default=str)
    except Exception:
        s = str(val)
    s = s.strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _default_task_state(session_id: str, items: dict[str, Any]) -> TaskState:
    obligations = _obligations_from_items(items)
    return TaskState(
        task_id="triage",
        session_id=session_id,
        status="active",
        active_step=None,
        pending_inputs=[],
        references={
            "active_workstreams": dict(items),
            "pending_obligations": obligations,
        },
    )


def _obligations_from_items(items: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "reference_id": wid,
            "tool": (entry.get("tool") or entry.get("handler") or ""),
            "kind": "awaiting_followup",
        }
        for wid, entry in items.items()
        if isinstance(entry, dict) and entry.get("status") == "waiting_for_user"
    ]


def _task_state_from_full_dict(data: dict[str, Any], fallback_session_id: str) -> TaskState:
    refs = data.get("references")
    if not isinstance(refs, dict):
        refs = {}
    return TaskState(
        task_id=str(data.get("task_id") or "triage"),
        session_id=str(data.get("session_id") or fallback_session_id),
        status=str(data.get("status") or "active"),
        active_step=data.get("active_step"),
        required_branches=list(data.get("required_branches") or []),
        completed_branches=list(data.get("completed_branches") or []),
        pending_inputs=list(data.get("pending_inputs") or []),
        references=dict(refs),
    )


def _task_state_to_dict(ts: TaskState) -> dict[str, Any]:
    return {
        "task_id": ts.task_id,
        "session_id": ts.session_id,
        "status": ts.status,
        "active_step": ts.active_step,
        "required_branches": list(ts.required_branches),
        "completed_branches": list(ts.completed_branches),
        "pending_inputs": list(ts.pending_inputs),
        "references": dict(ts.references or {}),
    }


def _deep_merge_workstreams(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base) if base else {}
    for wid, wentry in patch.items():
        if not isinstance(wentry, dict):
            out[str(wid)] = wentry
            continue
        prev = out.get(str(wid))
        if isinstance(prev, dict):
            out[str(wid)] = {**prev, **wentry}
        else:
            out[str(wid)] = dict(wentry)
    return out


def _deep_merge_references(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base) if base else {}
    for key, val in patch.items():
        if key == "active_workstreams" and isinstance(val, dict):
            out["active_workstreams"] = _deep_merge_workstreams(out.get("active_workstreams") or {}, val)
        elif key == "pending_obligations" and isinstance(val, list):
            out["pending_obligations"] = list(val)
        else:
            out[key] = deepcopy(val)
    return out


def _merge_task_state_patch(current: TaskState, patch: dict[str, Any]) -> TaskState:
    d = _task_state_to_dict(current)
    for k, v in patch.items():
        if k == "references" and isinstance(v, dict):
            d["references"] = _deep_merge_references(d.get("references") or {}, v)
        elif k in (
            "task_id",
            "session_id",
            "status",
            "active_step",
            "required_branches",
            "completed_branches",
            "pending_inputs",
        ):
            d[k] = v
    return _task_state_from_full_dict(d, current.session_id)


class WorkstreamRegistry(TaskStateStore):
    """
    Load/save the **active workstreams** map on the workspace document.

    Resolved the same way as ``AgentUtilities.get_active_workspace``: list workspaces for the
    thread, create if none, use the last row. Stored at top-level ``workstreams`` as a flat map
    ``{ reference_id: { ... } }``.

    Implements :class:`TaskStateStore` so :class:`Loop` can call ``get_task_state`` / ``patch_task_state``
    and optional ``after_tool_calls`` after scheduler tools return.
    """

    def __init__(
        self,
        session_controller: Any,
        portfolio: str,
        org: str,
        entity_type: str,
        entity_id: str,
        thread_id: str,
    ) -> None:
        self._ssc = session_controller
        self._portfolio = portfolio
        self._org = org
        self._entity_type = entity_type
        self._entity_id = entity_id
        self._thread_id = thread_id
        self._active_workspace: Optional[dict[str, Any]] = None

    def _fetch_active_workspace_item(self) -> Optional[dict[str, Any]]:
        try:
            workspaces_list = self._ssc.list_workspaces(
                self._portfolio,
                self._org,
                self._entity_type,
                self._entity_id,
                self._thread_id,
            )
            if not workspaces_list.get("success"):
                _logger.warning("workstreams: list_workspaces failed: %s", workspaces_list)
                return None

            items = list(workspaces_list.get("items") or [])
            if len(items) == 0:
                response = self._ssc.create_workspace(
                    self._portfolio,
                    self._org,
                    self._entity_type,
                    self._entity_id,
                    self._thread_id,
                    {},
                )
                if not response.get("success"):
                    _logger.warning("workstreams: create_workspace failed: %s", response)
                    return None
                workspaces_list = self._ssc.list_workspaces(
                    self._portfolio,
                    self._org,
                    self._entity_type,
                    self._entity_id,
                    self._thread_id,
                )
                if not workspaces_list.get("success"):
                    return None
                items = list(workspaces_list.get("items") or [])

            if not items:
                return None

            last = items[-1]
            return last if isinstance(last, dict) else None
        except Exception as e:
            _logger.warning("workstreams: _fetch_active_workspace_item: %s", e)
            return None

    def _ensure_active_workspace(self) -> Optional[dict[str, Any]]:
        if self._active_workspace is not None:
            return self._active_workspace
        self._active_workspace = self._fetch_active_workspace_item()
        return self._active_workspace

    def _read_items(self) -> dict[str, Any]:
        ws = self._ensure_active_workspace()
        if not ws:
            return {}

        return _persisted_workstreams_to_items_map(ws.get(WORKSPACE_WORKSTREAMS_KEY))

    def _write_items(self, items: dict[str, Any]) -> None:
        ws = self._ensure_active_workspace()
        if not ws:
            return
        wid = ws.get("_id")
        if not wid:
            return
        blob = dict(items)
        try:
            res = self._ssc.update_workspace(
                self._portfolio,
                self._org,
                self._entity_type,
                self._entity_id,
                self._thread_id,
                str(wid),
                {WORKSPACE_WORKSTREAMS_KEY: blob},
            )
            if not res.get("success"):
                _logger.warning("workstreams: update_workspace: %s", res)
                return
            ws[WORKSPACE_WORKSTREAMS_KEY] = blob
        except Exception as e:
            _logger.warning("workstreams: write failed: %s", e)

    def _save_task_state(self, session_id: str, ts: TaskState) -> None:
        if not self._ensure_active_workspace():
            _logger.warning("workstreams: skip save, no workspace for thread")
            return
        refs = ts.references or {}
        aw = refs.get("active_workstreams")
        if not isinstance(aw, dict):
            aw = {}
        obligations = _obligations_from_items(aw)
        ts.references = {
            **dict(refs),
            "active_workstreams": aw,
            "pending_obligations": obligations,
        }
        self._write_items(aw)

    def get_task_state(self, session_id: str) -> Optional[TaskState]:
        if not self._ensure_active_workspace():
            return None
        items = self._read_items()
        if not items:
            return _default_task_state(session_id, {})
        return _default_task_state(session_id, items)

    def save_task_state(self, task_state: TaskState) -> None:
        self._save_task_state(task_state.session_id, task_state)

    def patch_task_state(self, session_id: str, patch: dict[str, Any]) -> Optional[TaskState]:
        if not self._ensure_active_workspace():
            return None
        current = self.get_task_state(session_id) or _default_task_state(session_id, {})
        current.session_id = session_id
        current = _merge_task_state_patch(current, patch)
        self._save_task_state(session_id, current)
        return current

    def after_tool_calls(
        self,
        session_id: str,
        tool_calls: list[ToolCall],
        tool_results: list[ToolResult],
    ) -> None:
        """Update workstream entries from tool calls (e.g. ``agent_quotes`` + ``reference_id``)."""
        if len(tool_calls) != len(tool_results):
            return
        if not self._ensure_active_workspace():
            return

        items = dict(self._read_items())

        for tc, tr in zip(tool_calls, tool_results):
            handler = (tc.tool_name or "").strip()
            args = tc.arguments if isinstance(tc.arguments, dict) else {}
            ref = args.get("reference_id") or args.get("referenceId")
            if ref is None or str(ref).strip() == "":
                continue
            ref_s = str(ref).strip()
            label = args.get("label")
            entry = dict(items.get(ref_s) or {})
            entry["reference_id"] = ref_s
            entry["tool"] = handler
            entry["handler"] = handler
            if label is not None:
                entry["label"] = str(label)
            entry["last_message_preview"] = _preview(args.get("message"), 400)
            if tr.success:
                if _tool_result_requests_workstream_release(tr.result):
                    entry["status"] = "completed"
                else:
                    entry["status"] = "waiting_for_user"
                entry["last_result_preview"] = _preview(tr.result, 800)
                entry["last_error"] = None
            else:
                entry["status"] = "error"
                entry["last_error"] = str(tr.error or "")[:800]
            items[ref_s] = entry

        self._write_items(items)


class Workstreams(Context):
    """
    Same as :class:`Context`, plus one internal message listing **active workstreams** from the
    registry so the model can choose which ``reference_id`` / handler applies this turn.
    """

    def build_context(
        self,
        incoming_event: IncomingEvent,
        session_events: list[SessionEvent],
        task_state: Optional[TaskState],
        belief_facts: list[MemoryFact],
        journal_entries: list[JournalEntry],
        available_tools: list[ToolDefinition],
    ) -> ContextBundle:
        bundle = super().build_context(
            incoming_event,
            session_events,
            task_state,
            belief_facts,
            journal_entries,
            available_tools,
        )
        if not task_state:
            return bundle
        refs = task_state.references or {}
        aw = refs.get("active_workstreams")
        po = refs.get("pending_obligations")
        if not isinstance(aw, dict):
            aw = {}
        if not isinstance(po, list):
            po = []
        if not aw and not po:
            return bundle
        lines = [
            "Active workstreams (multi-step flows). Pick which reference_id applies this turn; "
            "call the listed handler/tool with that reference_id and the user's message.",
            "If any workstream has status waiting_for_user, you MUST call that handler with the "
            "same reference_id and the user's latest message before answering on your own.",
            f"active_workstreams: {json.dumps(aw, default=str)[:12000]}",
            f"pending_obligations: {json.dumps(po, default=str)[:8000]}",
        ]
        extra = PromptMessage(
            role="internal",
            content="\n".join(lines),
            metadata={"layer": "workstreams"},
        )
        messages = list(bundle.messages)
        insert_at = min(3, len(messages))
        messages.insert(insert_at, extra)
        return ContextBundle(messages=messages, tools=bundle.tools, diagnostics=bundle.diagnostics)
