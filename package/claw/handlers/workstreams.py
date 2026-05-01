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

import hashlib
import json
import logging
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional

from .class_prototypes import (
    ContextBundle,
    IncomingEvent,
    JournalEntry,
    LLMAdapter,
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
# Triage-owned default / active workstream key (hex); stamped onto tool calls server-side.
WORKSPACE_TRIAGE_FOCAL_KEY = "triage_focal_workstream_id"


def generate_triage_workstream_hex_id() -> str:
    """Opaque hex id for a triage workstream (32 nybbles, no dashes)."""
    return uuid.uuid4().hex


def ensure_reference_id() -> str:
    """Opaque id for a new workstream (UUID). Pass as ``reference_id`` to the handler/tool."""
    return str(uuid.uuid4())


def _collect_waiting_workstreams(task_state: Optional[TaskState]) -> list[tuple[str, dict[str, Any]]]:
    if not task_state or not task_state.references:
        return []
    aw = task_state.references.get("active_workstreams")
    if not isinstance(aw, dict):
        return []
    waiting: list[tuple[str, dict[str, Any]]] = []
    for rid, entry in aw.items():
        if isinstance(entry, dict) and entry.get("status") == "waiting_for_user":
            waiting.append((str(rid), entry))
    return waiting


def _waiting_reference_ids_from_items(items: dict[str, Any]) -> set[str]:
    return {
        str(k)
        for k, v in items.items()
        if isinstance(v, dict) and v.get("status") == "waiting_for_user"
    }


def _tool_call_raw_reference_id(args: dict[str, Any]) -> str:
    r = args.get("reference_id")
    return str(r).strip() if r is not None else ""


def _set_tool_reference_arguments(args: dict[str, Any], reference_id: str) -> None:
    args["reference_id"] = reference_id


def _normalize_fingerprint_token(text: Any) -> str:
    s = str(text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _legacy_trip_fingerprint_from_request(tr: dict[str, Any]) -> str:
    """Backward-compatible trip dedupe key (pipe-separated). Prefer ``fingerprint_payload``."""
    origin = _normalize_fingerprint_token(tr.get("origin") or tr.get("from"))
    destination = _normalize_fingerprint_token(tr.get("destination") or tr.get("to"))
    date = _normalize_fingerprint_token(tr.get("start_date") or tr.get("departure_date"))
    nights = str(tr.get("nights") or "").strip()
    adults = str(tr.get("adults") or "").strip()
    if not any((origin, destination, date, nights, adults)):
        return ""
    return "|".join((origin, destination, date, nights, adults))


def _canonicalize_for_fingerprint(val: Any) -> Any:
    if isinstance(val, dict):
        return {str(k): _canonicalize_for_fingerprint(v) for k, v in sorted(val.items(), key=lambda kv: str(kv[0]))}
    if isinstance(val, list):
        return [_canonicalize_for_fingerprint(x) for x in val]
    if isinstance(val, str):
        return _normalize_fingerprint_token(val)
    return val


def _hash_intent_fingerprint(schema: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(
        {"schema": schema, "payload": _canonicalize_for_fingerprint(payload)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _intent_fingerprint_from_tool_args(args: dict[str, Any]) -> str:
    """
    Domain-agnostic stream dedupe key.

    Precedence:
    1. ``intent_fingerprint`` if caller supplies it
    2. ``fingerprint_schema`` + ``fingerprint_payload`` (hashed)
    3. legacy ``trip_fingerprint`` / ``trip_request``
    """
    explicit = args.get("intent_fingerprint")
    if explicit is not None:
        s = str(explicit).strip().lower()
        if s:
            return s
    schema_raw = args.get("fingerprint_schema")
    payload = args.get("fingerprint_payload")
    if isinstance(payload, dict) and payload:
        schema = str(schema_raw or "generic.v1").strip() or "generic.v1"
        return _hash_intent_fingerprint(schema, payload)
    legacy_trip = args.get("trip_fingerprint")
    if legacy_trip is not None:
        s = str(legacy_trip).strip().lower()
        if s:
            return s
    tr = args.get("trip_request")
    if isinstance(tr, dict):
        return _legacy_trip_fingerprint_from_request(tr)
    return ""


def _workstream_intent_fingerprint_key(entry: dict[str, Any]) -> str:
    v = str(entry.get("intent_fingerprint") or entry.get("trip_fingerprint") or "").strip().lower()
    return v


def _parse_forced_workstream_selector_output(
    raw: dict[str, Any],
    valid_ids: set[str],
) -> tuple[Optional[str], bool]:
    """
    Parse waiting-workstream selector LLM output.

    Returns ``(reference_id_for_forced_tool, bump_focal)`` where ``bump_focal`` is True only when
    the model explicitly chose ``new_intent`` (start a new triage workstream id).
    """
    try:
        msg = (raw.get("choices") or [{}])[0].get("message") or {}
        content = str(msg.get("content") or "").strip()
        if not content or content.startswith("LLM error:"):
            return None, False
        data = json.loads(content)
        if not isinstance(data, dict):
            return None, False

        action = str(data.get("action") or "").strip().lower()
        rid_raw = data.get("reference_id")

        if action == "new_intent":
            _logger.debug("workstreams: selector LLM chose new_intent (no forced tool)")
            return None, True

        if action == "continue":
            if rid_raw is None:
                return None, False
            rid = str(rid_raw).strip()
            if rid not in valid_ids:
                _logger.warning(
                    "workstreams: selector LLM continue with unknown reference_id %r; skip forced route",
                    rid,
                )
                return None, False
            return rid, False

        # Legacy shape: {"reference_id": "..."} without action (older prompts / models)
        if not action and rid_raw is not None:
            rid = str(rid_raw).strip()
            return (rid if rid in valid_ids else None), False

        return None, False
    except Exception:
        return None, False


def _parse_forced_workstream_reference_id(
    raw: dict[str, Any],
    valid_ids: set[str],
) -> Optional[str]:
    chosen, _ = _parse_forced_workstream_selector_output(raw, valid_ids)
    return chosen


def _forced_tool_call_for_reference(
    by_id: dict[str, dict[str, Any]],
    chosen: str,
    user_text: str,
) -> list[ToolCall]:
    entry = by_id[chosen]
    tool = (entry.get("tool") or entry.get("handler") or "").strip()
    if not tool:
        return []
    return [
        ToolCall(
            tool_name=tool,
            arguments={"reference_id": chosen, "message": user_text},
            call_id=f"forced-workstream-{uuid.uuid4()}",
        )
    ]


@dataclass
class ForcedWorkstreamRouting:
    """Result of iteration-1 routing when workstreams are waiting for the user."""

    tool_calls: list[ToolCall]
    bump_focal_workstream: bool = False


def resolve_forced_workstream_routing(
    task_state: Optional[TaskState],
    incoming_event: IncomingEvent,
    llm: Optional[LLMAdapter] = None,
) -> ForcedWorkstreamRouting:
    """
    When one or more workstreams are ``waiting_for_user``, route the user turn via a small LLM
    call: ``action: continue`` plus ``reference_id`` forces that tool; ``action: new_intent`` sets
    ``bump_focal_workstream`` so triage allocates a new focal id before the main model runs.

    If any workstream is waiting, an ``LLMAdapter`` **must** be provided; otherwise this raises
    ``RuntimeError`` (no silent fallback).

    If the LLM returns an invalid ``reference_id`` or parsing fails, returns empty tool calls and
    no bump.
    """
    if incoming_event.event_type != "user_message":
        return ForcedWorkstreamRouting([])
    waiting = _collect_waiting_workstreams(task_state)
    if not waiting:
        return ForcedWorkstreamRouting([])

    if llm is None:
        raise RuntimeError(
            "workstream routing requires an LLM when one or more workstreams are waiting_for_user "
            "(configure Loop with a non-null llm adapter)"
        )

    payload = incoming_event.payload or {}
    user_text = str(payload.get("text") or payload.get("message") or "")

    by_id = {rid: entry for rid, entry in waiting}
    valid_ids = set(by_id.keys())

    candidates = []
    for ref_id, entry in waiting:
        candidates.append(
            {
                "reference_id": ref_id,
                "tool": (entry.get("tool") or entry.get("handler") or "").strip(),
                "label": entry.get("label"),
                "last_message_preview": entry.get("last_message_preview"),
                "last_result_preview": entry.get("last_result_preview"),
            }
        )

    system = (
        "You route the user's latest message when one or more multi-step workstreams are waiting "
        "for them.\n"
        '- If they are continuing, answering, or clarifying **within** one of those flows, '
        'respond with {"action": "continue", "reference_id": "<id>"} using the exact id from '
        "candidates.\n"
        "- If they are asking for something **separate** — a parallel task, unrelated tool, new "
        "topic, side question (e.g. distances between places, calendar dates, a different errand) "
        'while a trip or quote flow is still open — respond with {"action": "new_intent", '
        '"reference_id": null} so triage can start or route a new flow without hijacking the '
        "existing workstream.\n"
        "Use labels and last_message_preview / last_result_preview for context. "
        'Reply with JSON only; keys are always "action" and "reference_id".'
    )
    user_block = json.dumps({"user_message": user_text, "candidates": candidates}, default=str)

    try:
        bundle = ContextBundle(
            messages=[
                PromptMessage(role="system", content=system),
                PromptMessage(role="user", content=user_block),
            ],
            tools=[],
            response_format={"type": "json_object"},
        )
        raw = llm.complete(bundle)
        if not isinstance(raw, dict):
            return ForcedWorkstreamRouting([])
        chosen, bump = _parse_forced_workstream_selector_output(raw, valid_ids)
        if bump:
            return ForcedWorkstreamRouting([], bump_focal_workstream=True)
        if chosen:
            return ForcedWorkstreamRouting(_forced_tool_call_for_reference(by_id, chosen, user_text))
        return ForcedWorkstreamRouting([])
    except Exception as e:
        _logger.warning("workstreams: selector LLM failed: %s", e)
        return ForcedWorkstreamRouting([])


def forced_tool_calls_for_pending_workstream_reply(
    task_state: Optional[TaskState],
    incoming_event: IncomingEvent,
    llm: Optional[LLMAdapter] = None,
) -> list[ToolCall]:
    """Backward-compatible wrapper: returns only the forced tool calls list."""
    return resolve_forced_workstream_routing(task_state, incoming_event, llm=llm).tool_calls


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


def _extract_subagent_protocol(result: Any) -> Optional[dict[str, Any]]:
    if isinstance(result, dict):
        proto = result.get("subagent_protocol")
        if isinstance(proto, dict):
            return proto
        msgs = result.get("messages")
        if isinstance(msgs, list):
            for row in msgs:
                if (
                    isinstance(row, dict)
                    and row.get("_interface") == "subagent_protocol"
                    and isinstance((row.get("_out") or {}).get("content"), dict)
                ):
                    return (row.get("_out") or {}).get("content")
    if isinstance(result, list):
        for row in result:
            if (
                isinstance(row, dict)
                and row.get("_interface") == "subagent_protocol"
                and isinstance((row.get("_out") or {}).get("content"), dict)
            ):
                return (row.get("_out") or {}).get("content")
    return None


def _preview(val: Any, max_len: int) -> str:
    try:
        s = val if isinstance(val, str) else json.dumps(val, default=str)
    except Exception:
        s = str(val)
    s = s.strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _coerce_non_empty_trip_id(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or len(s) < 8:
        return None
    low = s.lower()
    if low.startswith("error") or "error in " in low[:80]:
        return None
    return s


def _extract_trip_id_from_tool_result(result: Any) -> Optional[str]:
    """
    Best-effort ``trip_id`` from scheduler canonical tool output.

    Covers NOMA-style handlers that return a string (e.g. ``check_for_trip_id``), a dict with
    top-level ``trip_id``, or nested ``output.trip_id`` (legacy / wrapped shapes).
    """
    if result is None:
        return None
    if isinstance(result, str):
        return _coerce_non_empty_trip_id(result)
    if isinstance(result, dict):
        for key in ("trip_id", "tripId"):
            tid = _coerce_non_empty_trip_id(result.get(key))
            if tid:
                return tid
        out = result.get("output")
        if isinstance(out, dict):
            for key in ("trip_id", "tripId"):
                tid = _coerce_non_empty_trip_id(out.get(key))
                if tid:
                    return tid
    return None


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

    def _read_focal(self) -> Optional[str]:
        ws = self._ensure_active_workspace()
        if not ws:
            return None
        raw = ws.get(WORKSPACE_TRIAGE_FOCAL_KEY)
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None

    def _write_focal(self, focal_id: str) -> None:
        ws = self._ensure_active_workspace()
        if not ws:
            return
        wid = ws.get("_id")
        if not wid:
            return
        s = str(focal_id).strip()
        if not s:
            return
        try:
            res = self._ssc.update_workspace(
                self._portfolio,
                self._org,
                self._entity_type,
                self._entity_id,
                self._thread_id,
                str(wid),
                {WORKSPACE_TRIAGE_FOCAL_KEY: s},
            )
            if not res.get("success"):
                _logger.warning("workstreams: update_workspace focal: %s", res)
                return
            ws[WORKSPACE_TRIAGE_FOCAL_KEY] = s
        except Exception as e:
            _logger.warning("workstreams: focal write failed: %s", e)

    def get_or_create_focal_workstream_id(self) -> str:
        """Persisted triage focal ``reference_id`` (hex); created once per workspace row."""
        existing = self._read_focal()
        if existing:
            return existing
        nid = generate_triage_workstream_hex_id()
        self._write_focal(nid)
        return nid

    def set_focal_workstream_id(self, reference_id: str) -> None:
        """Point triage focal at an existing workstream (e.g. forced ``continue``)."""
        s = str(reference_id).strip()
        if s:
            self._write_focal(s)

    def bump_focal_workstream_id(self) -> str:
        """Allocate a new focal id (parallel / ``new_intent`` path)."""
        nid = generate_triage_workstream_hex_id()
        self._write_focal(nid)
        return nid

    def after_forced_workstream_routing(self, routing: ForcedWorkstreamRouting) -> None:
        """Sync focal from iteration-1 waiting-workstream routing before context build."""
        if routing.tool_calls:
            args = routing.tool_calls[0].arguments if isinstance(routing.tool_calls[0].arguments, dict) else {}
            rid = args.get("reference_id")
            if rid:
                self.set_focal_workstream_id(str(rid).strip())
        elif routing.bump_focal_workstream:
            self.bump_focal_workstream_id()

    def apply_triage_focal_reference_to_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        """
        Normalize ``reference_id`` per tool call so parallel ``agent_quotes`` (e.g.
        two new trips) get **distinct** server hex ids, while **waiting_for_user** rows and other
        in-flight workstream keys are left as-is.

        - Id matching a **waiting** workstream → keep (selector / model chose the right continuation).
        - Id matching a non-completed row (e.g. ``error``) → keep.
        - Id matching **completed** or unknown LLM label → new hex (fresh flow).
        - **Omitted** id: if this batch has a single workstream tool call, use triage focal
          (``get_or_create``); if **multiple** such calls, allocate a new hex per empty call so
          trips do not collapse.
        - Duplicate resolved ids within the same batch → second and later calls get fresh hexes.

        Updates ``triage_focal_workstream_id`` on the workspace to the **last** resolved id in the
        batch (hint for the next turn).
        """
        if not tool_calls or not self._ensure_active_workspace():
            return

        ws_calls: list[ToolCall] = []
        for tc in tool_calls:
            args = tc.arguments if isinstance(tc.arguments, dict) else None
            if not args:
                continue
            name = (tc.tool_name or "").strip()
            has_ref = "reference_id" in args
            if name == "agent_quotes" or has_ref:
                ws_calls.append(tc)

        if not ws_calls:
            return

        items = self._read_items()
        waiting_ids = _waiting_reference_ids_from_items(items)
        by_fingerprint: dict[str, str] = {}
        for rid, entry in items.items():
            if not isinstance(entry, dict):
                continue
            st = str(entry.get("status") or "")
            if st == "completed":
                continue
            fp = _workstream_intent_fingerprint_key(entry)
            if fp and fp not in by_fingerprint:
                by_fingerprint[fp] = str(rid)
        n_ws = len(ws_calls)
        assigned_in_batch: set[str] = set()
        last_resolved: Optional[str] = None

        for tc in ws_calls:
            args = tc.arguments
            if not isinstance(args, dict):
                continue
            raw = _tool_call_raw_reference_id(args)
            fp = _intent_fingerprint_from_tool_args(args)
            if fp:
                args["intent_fingerprint"] = fp
            resolved: Optional[str] = None

            if raw in waiting_ids:
                resolved = raw
            elif raw:
                entry = items.get(raw)
                if isinstance(entry, dict):
                    st = str(entry.get("status") or "")
                    if st == "completed":
                        resolved = None
                    else:
                        resolved = raw
                else:
                    resolved = None

            if resolved is None and fp:
                existing = by_fingerprint.get(fp)
                if existing:
                    resolved = existing

            if resolved is None:
                if not raw:
                    resolved = (
                        self.get_or_create_focal_workstream_id()
                        if n_ws == 1
                        else generate_triage_workstream_hex_id()
                    )
                else:
                    resolved = generate_triage_workstream_hex_id()

            while resolved in assigned_in_batch and not (fp and by_fingerprint.get(fp) == resolved):
                resolved = generate_triage_workstream_hex_id()

            assigned_in_batch.add(resolved)
            if fp:
                by_fingerprint[fp] = resolved
            _set_tool_reference_arguments(args, resolved)
            last_resolved = resolved

        if last_resolved:
            self.set_focal_workstream_id(last_resolved)

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
        ts = _default_task_state(session_id, items if items else {})
        focal = self.get_or_create_focal_workstream_id()
        refs = dict(ts.references or {})
        refs["triage_focal_workstream_id"] = focal
        ts.references = refs
        return ts

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
        """
        Update workstream entries from tool calls (e.g. ``agent_quotes`` + ``reference_id``).

        When the tool's canonical ``output`` includes a ``trip_id`` (e.g. ``add_flight`` /
        ``add_hotel`` / ``check_for_trip_id``), the entry stores ``trip_id`` so triage keeps a
        durable pointer to the ``noma_travels`` document for this flow (``search_trips``, etc.).
        """
        if len(tool_calls) != len(tool_results):
            return
        if not self._ensure_active_workspace():
            return

        items = dict(self._read_items())

        for tc, tr in zip(tool_calls, tool_results):
            handler = (tc.tool_name or "").strip()
            args = tc.arguments if isinstance(tc.arguments, dict) else {}
            ref = args.get("reference_id")
            if ref is None or str(ref).strip() == "":
                continue
            ref_s = str(ref).strip()
            label = args.get("label")
            entry = dict(items.get(ref_s) or {})
            entry["reference_id"] = ref_s
            entry["tool"] = handler
            entry["handler"] = handler
            fp = _intent_fingerprint_from_tool_args(args)
            if fp:
                entry["intent_fingerprint"] = fp
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

            proto = _extract_subagent_protocol(tr.result)
            if isinstance(proto, dict):
                entry["subagent_protocol"] = proto
                upd = proto.get("update")
                if isinstance(upd, dict):
                    intention = upd.get("intention")
                    state = upd.get("state")
                    msg_for_user = upd.get("message_for_user")
                    if intention:
                        entry["intention"] = str(intention)
                    if state:
                        entry["subagent_state"] = str(state)
                    if msg_for_user:
                        entry["message_for_user"] = str(msg_for_user)

            tid = _extract_trip_id_from_tool_result(tr.result)
            if tid:
                entry["trip_id"] = tid

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
        focal = refs.get("triage_focal_workstream_id")
        focal_s = str(focal).strip() if focal is not None else ""
        aw = refs.get("active_workstreams")
        po = refs.get("pending_obligations")
        if not isinstance(aw, dict):
            aw = {}
        if not isinstance(po, list):
            po = []
        if not aw and not po and not focal_s:
            return bundle
        lines = []
        if focal_s:
            lines.extend(
                [
                    "Triage focal workstream: default hex reference_id for this thread. Before tools "
                    "run, the runtime normalizes reference_id on agent_quotes (and other calls that "
                    "already pass reference_id): waiting / in-flight ids are kept; completed or "
                    "unknown labels get new hex ids; several agent_quotes in one turn each get "
                    "distinct ids (omitted id on a lone call uses this focal). Intent fingerprint "
                    "(``intent_fingerprint`` or hashed ``fingerprint_payload``) matches reuse the same "
                    "in-flight stream instead of creating a duplicate. Focal "
                    "is updated to the last resolved id after each tool batch.",
                    f"triage_focal_workstream_id: {focal_s}",
                ]
            )
        if aw or po:
            lines.extend(
                [
                    "Active workstreams (multi-step flows). Pick which reference_id applies this turn; "
                    "call the listed handler/tool with that reference_id and the user's message.",
                    "If the user is continuing a flow that is waiting_for_user, prefer calling that handler "
                    "with the same reference_id and their latest message.",
                    "Do not split one trip intent into multiple workstreams. A single trip with flights, "
                    "hotel, and return leg stays in one stream.",
                    "Create multiple workstreams only when the user explicitly asks for multiple distinct trips.",
                    "If the user is starting a separate or parallel task (unrelated question, new tool path), "
                    "call the appropriate handler with a new reference_id instead of reusing a waiting one.",
                    f"active_workstreams: {json.dumps(aw, default=str)[:12000]}",
                    f"pending_obligations: {json.dumps(po, default=str)[:8000]}",
                ]
            )
        extra = PromptMessage(
            role="internal",
            content="\n".join(lines),
            metadata={"layer": "workstreams"},
        )
        messages = list(bundle.messages)
        insert_at = min(3, len(messages))
        messages.insert(insert_at, extra)
        return ContextBundle(messages=messages, tools=bundle.tools, diagnostics=bundle.diagnostics)
