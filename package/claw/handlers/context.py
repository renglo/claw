from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from .class_prototypes import (
    ContextBundle,
    IncomingEvent,
    JournalEntry,
    MemoryFact,
    PromptMessage,
    SessionEvent,
    TaskState,
    ToolDefinition,
)


class Context:
    """
    Assembles ``ContextBundle`` from layers described in claw_specs.

    Callers load beliefs (``Beliefs``), journal (``Journal``), session turns
    (``Sessions``), and tool definitions (typically ``schd_tools`` via
    ``DataController.get_a_b``) before invoking ``build_context``.
    """

    def __init__(
        self,
        system_identity: str = "You are a capable assistant operating inside the Claw runtime.",
        policy_hint: str = "Follow user intent, use tools when they reduce error, and keep answers concise.",
        max_session_events: int = 40,
        max_beliefs: int = 30,
        max_journal: int = 15,
    ) -> None:
        self._system_identity = system_identity
        self._policy_hint = policy_hint
        self._max_session_events = max_session_events
        self._max_beliefs = max_beliefs
        self._max_journal = max_journal

    def build_context(
        self,
        incoming_event: IncomingEvent,
        session_events: list[SessionEvent],
        task_state: Optional[TaskState],
        belief_facts: list[MemoryFact],
        journal_entries: list[JournalEntry],
        available_tools: list[ToolDefinition],
    ) -> ContextBundle:
        messages: list[PromptMessage] = [
            PromptMessage(role="system", content=self._system_identity, metadata={"layer": "identity"}),
            PromptMessage(role="system", content=self._policy_hint, metadata={"layer": "policy"}),
        ]
        now = datetime.now().astimezone()
        messages.append(
            PromptMessage(
                role="system",
                content=(
                    f"The current date and time is {now.strftime('%A, %Y-%m-%d %H:%M:%S %Z')} "
                    f"(ISO: {now.isoformat(timespec='seconds')}). "
                    "Use this when interpreting relative dates such as today, tomorrow, next week, or travel dates."
                ),
                metadata={"layer": "clock"},
            )
        )
        if task_state:
            ts_line = (
                f"Task {task_state.task_id} status={task_state.status} "
                f"step={task_state.active_step!r} pending={task_state.pending_inputs}"
            )
            messages.append(PromptMessage(role="internal", content=ts_line, metadata={"layer": "task_state"}))

        if belief_facts:
            lines = []
            for b in belief_facts[: self._max_beliefs]:
                lines.append(
                    f"- ({b.confidence:.2f}) {b.subject} / {b.predicate} => {b.value}  tags={b.tags}"
                )
            messages.append(
                PromptMessage(
                    role="system",
                    content="Beliefs:\n" + "\n".join(lines),
                    metadata={"layer": "beliefs"},
                )
            )

        if journal_entries:
            lines = []
            for j in journal_entries[: self._max_journal]:
                lines.append(f"- [{j.journal_date}] {j.summary}")
            messages.append(
                PromptMessage(
                    role="system",
                    content="Recent journal:\n" + "\n".join(lines),
                    metadata={"layer": "journal"},
                )
            )

        _ui_only = frozenset({"claw_stream", "claw_signal", "claw_subagent_message"})

        for ev in session_events:
            et = ev.event_type
            if et in _ui_only:
                continue
            if et == "user_message":
                ut = ev.payload.get("text") or ev.payload.get("message") or ""
                messages.append(
                    PromptMessage(
                        role="user",
                        content=str(ut),
                        metadata={"layer": "session", "event_id": ev.event_id},
                    )
                )
            elif et == "assistant_message":
                at = ev.payload.get("text") or ""
                messages.append(
                    PromptMessage(
                        role="assistant",
                        content=str(at),
                        metadata={"layer": "session", "event_id": ev.event_id},
                    )
                )
            elif et == "tool_call":
                summary = json.dumps(ev.payload, default=str)[:4000]
                messages.append(
                    PromptMessage(
                        role="internal",
                        content=f"Tool call: {summary}",
                        metadata={"layer": "session", "event_id": ev.event_id},
                    )
                )
            elif et == "tool_result":
                summary = json.dumps(ev.payload, default=str)[:4000]
                messages.append(
                    PromptMessage(
                        role="internal",
                        content=f"Tool result: {summary}",
                        metadata={"layer": "session", "event_id": ev.event_id},
                    )
                )
            else:
                summary = json.dumps(
                    {"type": ev.event_type, "payload": ev.payload},
                    default=str,
                )[:4000]
                messages.append(
                    PromptMessage(
                        role="internal",
                        content=f"Session event {ev.event_id}: {summary}",
                        metadata={"layer": "session", "event_id": ev.event_id},
                    )
                )

        diag: dict[str, Any] = {
            "session_events_used": len(session_events),
            "beliefs_used": len(belief_facts),
            "journal_used": len(journal_entries),
            "tools_offered": len(available_tools),
        }
        return ContextBundle(messages=messages, tools=available_tools, diagnostics=diag)

    def select_session_events(
        self,
        incoming_event: IncomingEvent,
        all_session_events: list[SessionEvent],
        task_state: Optional[TaskState],
    ) -> list[SessionEvent]:
        del incoming_event, task_state
        ordered = sorted(all_session_events, key=lambda e: e.timestamp)
        return ordered[-self._max_session_events :]

    def select_beliefs(
        self,
        incoming_event: IncomingEvent,
        task_state: Optional[TaskState],
        all_beliefs: list[MemoryFact],
    ) -> list[MemoryFact]:
        q = (incoming_event.payload.get("text") or incoming_event.payload.get("query") or "").lower()
        scored: list[tuple[float, MemoryFact]] = []
        for b in all_beliefs:
            score = float(b.confidence)
            blob = f"{b.subject} {b.predicate} {b.value}".lower()
            if q and q in blob:
                score += 2.0
            if task_state and task_state.task_id in (b.tags or []):
                score += 0.5
            scored.append((score, b))
        scored.sort(key=lambda x: -x[0])
        return [b for _, b in scored[: self._max_beliefs]]

    def select_journal_entries(
        self,
        incoming_event: IncomingEvent,
        task_state: Optional[TaskState],
        all_journal_entries: list[JournalEntry],
    ) -> list[JournalEntry]:
        del task_state
        today = datetime.utcnow().date()
        yesterday = today - timedelta(days=1)
        prefer = {str(today), str(yesterday)}
        q = (incoming_event.payload.get("text") or "").lower()
        scored: list[tuple[float, JournalEntry]] = []
        for j in all_journal_entries:
            score = 0.0
            if j.journal_date in prefer:
                score += 2.0
            if q and q in j.summary.lower():
                score += 1.5
            scored.append((score, j))
        scored.sort(key=lambda x: -x[0])
        return [j for _, j in scored[: self._max_journal]]

    def select_tools(
        self,
        incoming_event: IncomingEvent,
        task_state: Optional[TaskState],
        all_tools: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        subset = incoming_event.payload.get("tool_subset")
        if isinstance(subset, list) and subset:
            names = set(subset)
            return [t for t in all_tools if t.tool_name in names]
        if not task_state:
            return all_tools
        active = (task_state.references or {}).get("allowed_tools")
        if isinstance(active, list) and active:
            names = set(active)
            return [t for t in all_tools if t.tool_name in names]
        return all_tools