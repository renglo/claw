from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from .class_prototypes import SessionEvent


SESSION_KEY_SEP = "|"

# Message-roll event types: stored as ``_out`` = {role, content} (content may be str or dict).
ROLL_EVENT_TYPES = frozenset(
    {
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "claw_stream",
        "claw_signal",
        "claw_subagent_message",
    }
)


def format_session_key(entity_type: str, entity_id: str, thread_id: str) -> str:
    return SESSION_KEY_SEP.join([entity_type, entity_id, thread_id])


def parse_session_key(session_id: str) -> tuple[str, str, str]:
    parts = session_id.split(SESSION_KEY_SEP, 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid session_id (expected entity_type|entity_id|thread): {session_id!r}")
    return parts[0], parts[1], parts[2]


RING_SESSION_META = "claw_sessions"


def _sanitize_for_dynamo(obj: Any) -> Any:
    """Recursively make values safe for DynamoDB (avoid raw floats; normalize nested data)."""
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_dynamo(v) for v in obj]
    if isinstance(obj, float):
        return str(obj)
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


class Sessions:
    """
    Session ledger aligned with ``SessionController`` turns and optional
    metadata in ``claw_session``.

    Session id format: ``entity_type|entity_id|thread_id`` (``|`` must not
    appear inside components).

    Turn ``events`` rows use:
    - ``_type``: ``event_type`` string (e.g. ``assistant_message``).
    - ``_out``: for roll types, ``{"role", "content"}``; else the event body as a JSON object.
    - ``_meta``: ``event_id``, ``session_id``, ``timestamp``, plus optional ``SessionEvent.metadata``.
    """

    def __init__(
        self,
        session_controller: Any,
        portfolio: str,
        org: str,
        entity_type: str,
        entity_id: str,
        thread_id: str,
        data_controller: Any | None = None,
    ) -> None:
        self._ssc = session_controller
        self._portfolio = portfolio
        self._org = org
        self._entity_type = entity_type
        self._entity_id = entity_id
        self._thread_id = thread_id
        self._dc = data_controller
        self.session_id = format_session_key(entity_type, entity_id, thread_id)
        self._active_turn_id: str | None = None

    def _meta_id(self) -> str:
        return hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:40]

    def _load_meta_blob(self) -> dict[str, Any]:
        if not self._dc:
            return {}
        doc = self._dc.get_a_b_c(self._portfolio, self._org, RING_SESSION_META, self._meta_id())
        if doc.get("success") is False or "_id" not in doc:
            return {}
        return doc.get("metadata") or doc.get("meta") or {}

    def _save_meta_blob(self, meta: dict[str, Any]) -> None:
        if not self._dc:
            return
        mid = self._meta_id()
        existing = self._dc.get_a_b_c(self._portfolio, self._org, RING_SESSION_META, mid)
        payload = {"_id": mid, "session_id": self.session_id, "metadata": meta}
        if existing.get("success") is False:
            self._dc.post_a_b(self._portfolio, self._org, RING_SESSION_META, payload)
        else:
            self._dc.put_a_b_c(self._portfolio, self._org, RING_SESSION_META, mid, {"metadata": meta})

    def create_session(
        self,
        session_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if session_id != self.session_id:
            raise ValueError("session_id does not match bound Sessions key")
        meta = metadata or {}
        self._save_meta_blob(meta)

    def create_turn(self, context_payload: dict[str, Any], events: Optional[list] = None) -> str:
        """Creates a new turn document; returns ``turn_id`` (``_id``)."""
        ctx = dict(context_payload)
        if "public_user" not in ctx:
            ctx["public_user"] = False
        payload = {"context": ctx, "events": events or []}
        res = self._ssc.create_turn(
            self._portfolio,
            self._org,
            self._entity_type,
            self._entity_id,
            self._thread_id,
            payload,
        )
        if not res.get("success"):
            raise RuntimeError(res.get("message", "create_turn failed"))
        doc = res.get("document") or {}
        turn_id = str(doc.get("_id", ""))
        self._active_turn_id = turn_id
        blob = self._load_meta_blob()
        blob["active_turn_id"] = turn_id
        self._save_meta_blob(blob)
        return turn_id

    def get_active_turn_id(self) -> Optional[str]:
        if self._active_turn_id:
            return self._active_turn_id
        blob = self._load_meta_blob()
        return blob.get("active_turn_id")

    def update_turn(self, turn_id: str, update: dict[str, Any], call_id: Any = False) -> dict[str, Any]:
        return self._ssc.update_turn(
            self._portfolio,
            self._org,
            self._entity_type,
            self._entity_id,
            self._thread_id,
            turn_id,
            update,
            call_id=call_id,
        )

    def _base_meta(self, event: SessionEvent) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "event_id": event.event_id,
            "session_id": event.session_id,
            "timestamp": event.timestamp.isoformat(),
        }
        if event.metadata:
            meta.update(event.metadata)
        return meta

    def _roll_role(self, event_type: str) -> str:
        if event_type == "user_message":
            return "user"
        if event_type == "assistant_message":
            return "assistant"
        if event_type == "tool_call":
            return "assistant"
        if event_type == "tool_result":
            return "tool"
        if event_type in ("claw_stream", "claw_signal", "claw_subagent_message"):
            return "assistant"
        return "system"

    def _event_to_message(self, event: SessionEvent) -> dict[str, Any]:
        et = event.event_type
        meta = self._base_meta(event)

        if et in ROLL_EVENT_TYPES:
            if et in (
                "user_message",
                "assistant_message",
                "claw_stream",
                "claw_signal",
                "claw_subagent_message",
            ):
                text = event.payload.get("text")
                if text is None:
                    text = event.payload.get("message", "")
                content: Any = str(text)
            elif et == "tool_call":
                content = _sanitize_for_dynamo(
                    {
                        "tool": event.payload.get("tool", ""),
                        "arguments": event.payload.get("arguments") or {},
                        "call_id": event.payload.get("call_id"),
                    }
                )
            else:  # tool_result
                content = _sanitize_for_dynamo(
                    {
                        "tool": event.payload.get("tool", ""),
                        "call_id": event.payload.get("call_id"),
                        "success": event.payload.get("success"),
                        "result": event.payload.get("result"),
                        "error": event.payload.get("error"),
                    }
                )
            row = {
                "_type": et,
                "_out": {
                    "role": self._roll_role(et),
                    "content": content,
                },
                "_meta": _sanitize_for_dynamo(meta),
            }
            return _sanitize_for_dynamo(row)

        body = _sanitize_for_dynamo(dict(event.payload))
        row = {
            "_type": et,
            "_out": body,
            "_meta": _sanitize_for_dynamo(meta),
        }
        return _sanitize_for_dynamo(row)

    def _parse_timestamp(self, raw: Any) -> datetime:
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return datetime.utcnow()
        return datetime.utcnow()

    def _message_to_event(self, session_id: str, msg: dict[str, Any]) -> Optional[SessionEvent]:
        legacy_type = msg.get("_type")
        if legacy_type == "claw_session_event":
            return self._legacy_claw_session_event_to_event(session_id, msg)

        et = legacy_type
        if not et or not isinstance(et, str):
            return None

        meta = msg.get("_meta") if isinstance(msg.get("_meta"), dict) else {}
        out = msg.get("_out")
        if not isinstance(out, dict):
            out = {}

        event_id = str(meta.get("event_id") or uuid.uuid4())
        sid = str(meta.get("session_id") or session_id)
        ts = self._parse_timestamp(meta.get("timestamp"))

        extra_meta = {k: v for k, v in meta.items() if k not in ("event_id", "session_id", "timestamp")}

        if et in ROLL_EVENT_TYPES:
            content = out.get("content")
            if et in (
                "user_message",
                "assistant_message",
                "claw_stream",
                "claw_signal",
                "claw_subagent_message",
            ):
                payload = {"text": content if isinstance(content, str) else json.dumps(content, default=str)}
            elif et == "tool_call":
                if isinstance(content, dict):
                    args = content.get("arguments")
                    payload = {
                        "tool": content.get("tool", ""),
                        "arguments": args if isinstance(args, dict) else {},
                        "call_id": content.get("call_id"),
                    }
                else:
                    payload = {}
            else:  # tool_result
                if isinstance(content, dict):
                    payload = {
                        "tool": content.get("tool", ""),
                        "call_id": content.get("call_id"),
                        "success": content.get("success"),
                        "result": content.get("result"),
                        "error": content.get("error"),
                    }
                else:
                    payload = {}
            return SessionEvent(
                event_id=event_id,
                session_id=sid,
                event_type=et,
                timestamp=ts,
                payload=payload,
                metadata=extra_meta,
            )

        return SessionEvent(
            event_id=event_id,
            session_id=sid,
            event_type=et,
            timestamp=ts,
            payload=dict(out),
            metadata=extra_meta,
        )

    def _legacy_claw_session_event_to_event(self, session_id: str, msg: dict[str, Any]) -> Optional[SessionEvent]:
        out = msg.get("_out") or {}
        raw = out.get("content")
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            data = raw
        else:
            return None
        ts = data.get("timestamp")
        if isinstance(ts, str):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                t = datetime.utcnow()
        else:
            t = datetime.utcnow()
        return SessionEvent(
            event_id=str(data.get("event_id", uuid.uuid4())),
            session_id=str(data.get("session_id", session_id)),
            event_type=str(data.get("event_type", "unknown")),
            timestamp=t,
            payload=dict(data.get("payload") or {}),
            metadata={},
        )

    def append_event(self, event: SessionEvent) -> None:
        turn_id = self.get_active_turn_id()
        if not turn_id:
            raise RuntimeError("No active turn; call create_turn first")
        self.update_turn(turn_id, self._event_to_message(event), call_id=False)

    def get_events(
        self,
        session_id: str,
        limit: Optional[int] = None,
        since_event_id: Optional[str] = None,
    ) -> list[SessionEvent]:
        if session_id != self.session_id:
            raise ValueError("session_id mismatch")
        res = self._ssc.list_turns(
            self._portfolio,
            self._org,
            self._entity_type,
            self._entity_id,
            self._thread_id,
            False,
        )
        if not res.get("success"):
            return []
        events: list[SessionEvent] = []
        for turn in res.get("items", []):
            for m in turn.get("events") or []:
                ev = self._message_to_event(session_id, m)
                if ev:
                    events.append(ev)
        if since_event_id:
            try:
                idx = next(i for i, e in enumerate(events) if e.event_id == since_event_id)
                events = events[idx + 1 :]
            except StopIteration:
                pass
        if limit is not None:
            events = events[-limit:]
        return events

    def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        if session_id != self.session_id:
            raise ValueError("session_id mismatch")
        return self._load_meta_blob()

    def update_session_metadata(self, session_id: str, patch: dict[str, Any]) -> None:
        if session_id != self.session_id:
            raise ValueError("session_id mismatch")
        cur = self._load_meta_blob()
        cur.update(patch)
        self._save_meta_blob(cur)

    @staticmethod
    def derive_session_id(
        agent_name: str,
        channel: str,
        account_id: Optional[str],
        peer_id: Optional[str],
        thread_id: Optional[str],
    ) -> str:
        basis = "|".join(
            [
                agent_name or "",
                channel or "",
                account_id or "",
                peer_id or "",
                thread_id or "",
            ]
        )
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
        return f"derived-{digest}"
