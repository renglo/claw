from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from .class_prototypes import CompactionResult, SessionEvent, TaskState


RING_COMPACTION = "claw_compaction"

_DEFAULT_EVENT_BUDGET = 80


class Compaction:
    """
    Session compaction with optional memory flush via injected ``Beliefs`` / ``Journal``.

    Records each pass in ring ``claw_compaction``.
    """

    def __init__(
        self,
        data_controller: Any,
        portfolio: str,
        org: str,
        beliefs: Any | None = None,
        journal: Any | None = None,
        event_budget: int = _DEFAULT_EVENT_BUDGET,
    ) -> None:
        self._dc = data_controller
        self._portfolio = portfolio
        self._org = org
        self._beliefs = beliefs
        self._journal = journal
        self._event_budget = event_budget

    def should_compact(
        self,
        session_id: str,
        session_events: list[SessionEvent],
        task_state: Optional[TaskState],
    ) -> bool:
        del session_id, task_state
        return len(session_events) > self._event_budget

    def flush_memory_before_compaction(
        self,
        session_id: str,
        session_events: list[SessionEvent],
    ) -> dict[str, Any]:
        belief_payloads: list[dict[str, Any]] = []
        journal_payloads: list[dict[str, Any]] = []
        for ev in session_events:
            if ev.event_type == "belief_candidate" and isinstance(ev.payload, dict):
                belief_payloads.append(ev.payload)
            if ev.event_type == "journal_candidate" and isinstance(ev.payload, dict):
                journal_payloads.append(ev.payload)

        promoted_beliefs: list[str] = []
        promoted_journal: list[str] = []

        for bp in belief_payloads:
            if self._beliefs:
                fact = self._beliefs.write_fact(bp, bp.get("source_event_ids") or [])
                promoted_beliefs.append(fact.fact_id)

        for jp in journal_payloads:
            if self._journal:
                entry = self._journal.append_entry(
                    journal_date=str(jp.get("journal_date") or datetime.utcnow().date().isoformat()),
                    summary=str(jp.get("summary", "")),
                    session_id=session_id,
                    source_event_ids=list(jp.get("source_event_ids") or []),
                    tags=jp.get("tags"),
                )
                promoted_journal.append(entry.entry_id)

        return {
            "belief_promotions": belief_payloads,
            "journal_promotions": journal_payloads,
            "promoted_fact_ids": promoted_beliefs,
            "promoted_journal_entry_ids": promoted_journal,
        }

    def compact_session(
        self,
        session_id: str,
        session_events: list[SessionEvent],
        task_state: Optional[TaskState],
    ) -> CompactionResult:
        del task_state
        ordered = sorted(session_events, key=lambda e: e.timestamp)
        cutoff = max(1, len(ordered) // 2)
        older = ordered[:cutoff]
        kept = ordered[cutoff:]
        compacted_ids = [e.event_id for e in older]
        summary_text = json.dumps(
            [{"t": e.event_type, "p": e.payload} for e in older],
            default=str,
        )[:12000]
        summary_event = SessionEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            event_type="compaction_summary",
            timestamp=datetime.utcnow(),
            payload={"summary": summary_text, "covered_event_ids": compacted_ids, "kept_count": len(kept)},
        )
        flush = self.flush_memory_before_compaction(session_id, older)
        rec = {
            "session_id": session_id,
            "compacted_event_ids": compacted_ids,
            "summary_excerpt": summary_text[:2000],
            "flush": flush,
        }
        result, _st = self._dc.post_a_b(
            self._portfolio,
            self._org,
            RING_COMPACTION,
            {"record": rec, "time": datetime.utcnow().isoformat() + "Z"},
        )
        if not result.get("success"):
            pass
        return CompactionResult(
            session_id=session_id,
            compacted_event_ids=compacted_ids,
            summary_event=summary_event,
            promoted_fact_ids=list(flush.get("promoted_fact_ids") or []),
            promoted_journal_entry_ids=list(flush.get("promoted_journal_entry_ids") or []),
        )

    def build_hot_context_view(
        self,
        session_id: str,
        session_events: list[SessionEvent],
        task_state: Optional[TaskState],
    ) -> list[SessionEvent]:
        del session_id
        hot_types = {
            "assistant_message",
            "tool_call",
            "tool_result",
            "subagent_signal",
            "user_message",
        }
        ordered = sorted(session_events, key=lambda e: e.timestamp)
        tail = ordered[-30:]
        if task_state and task_state.active_step:
            tail.extend(
                e
                for e in ordered
                if e.event_type in hot_types and e not in tail
            )
        seen: set[str] = set()
        out: list[SessionEvent] = []
        for e in sorted(tail, key=lambda x: x.timestamp):
            if e.event_id not in seen:
                seen.add(e.event_id)
                out.append(e)
        return out
