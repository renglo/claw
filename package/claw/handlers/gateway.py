from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .class_prototypes import IncomingEvent, SubAgentSignal
from .sessions import format_session_key


class Gateway:
    """
    Routes inbound chat traffic and internal signals into the React ``Loop``.
    """

    def __init__(
        self,
        loop: Any,
        subagents: Any,
        portfolio: str,
        org: str,
        entity_type: str,
        entity_id: str,
        default_thread_id: str = "main",
    ) -> None:
        self._loop = loop
        self._subagents = subagents
        self._portfolio = portfolio
        self._org = org
        self._entity_type = entity_type
        self._entity_id = entity_id
        self._default_thread_id = default_thread_id

    def handle_incoming_message(
        self,
        agent_name: str,
        channel: str,
        payload: dict[str, Any],
        account_id: Optional[str] = None,
        peer_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        del account_id, peer_id
        tid = thread_id or self._default_thread_id
        routed = self._subagents.route_thread_message(tid, payload)
        if routed:
            session_id = routed
        else:
            session_id = format_session_key(self._entity_type, self._entity_id, tid)
        body = dict(payload)
        body.setdefault("text", body.get("message", ""))
        body.setdefault("context", {"public_user": body.get("public_user", False)})
        body["agent_name"] = agent_name
        body["channel"] = channel
        ev = IncomingEvent(
            event_type="user_message",
            session_id=session_id,
            payload=body,
            timestamp=datetime.utcnow(),
        )
        return self._loop.run_turn(ev)

    def handle_internal_signal(self, signal: SubAgentSignal) -> dict[str, Any]:
        payload = dict(signal.payload)
        payload.setdefault("signal", signal.signal_type)
        ev = IncomingEvent(
            event_type="subagent_signal",
            session_id=signal.target_session_id,
            payload=payload,
            timestamp=signal.timestamp,
        )
        return self._loop.run_turn(ev)

    def handle_scheduled_event(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        ev = IncomingEvent(
            event_type="scheduled",
            session_id=session_id,
            payload=payload,
            timestamp=datetime.utcnow(),
        )
        return self._loop.run_turn(ev)


class RuntimeCoordinator:
    """Optional wiring surface for dependency access (extension bootstrap)."""

    def __init__(
        self,
        context_engine: Any,
        react_loop: Any,
        belief_system: Any,
        journal: Any,
        subagents: Any,
        sessions: Any,
        compaction: Any,
        task_state_store: Any,
        tool_registry: Any,
        llm_adapter: Any,
    ) -> None:
        self._context_engine = context_engine
        self._react_loop = react_loop
        self._belief_system = belief_system
        self._journal = journal
        self._subagents = subagents
        self._sessions = sessions
        self._compaction = compaction
        self._task_state_store = task_state_store
        self._tool_registry = tool_registry
        self._llm_adapter = llm_adapter

    def get_context_engine(self) -> Any:
        return self._context_engine

    def get_react_loop(self) -> Any:
        return self._react_loop

    def get_belief_system(self) -> Any:
        return self._belief_system

    def get_journal(self) -> Any:
        return self._journal

    def get_subagents(self) -> Any:
        return self._subagents

    def get_sessions(self) -> Any:
        return self._sessions

    def get_compaction(self) -> Any:
        return self._compaction

    def get_task_state_store(self) -> Any:
        return self._task_state_store

    def get_tool_registry(self) -> Any:
        return self._tool_registry

    def get_llm_adapter(self) -> Any:
        return self._llm_adapter
