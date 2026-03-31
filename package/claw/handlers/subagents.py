from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Literal, Optional

from .class_prototypes import SubAgentMessage, SubAgentSignal, WorkerSessionBinding
from .sessions import format_session_key, parse_session_key


class SubAgents:
    """
    Parent/worker subagent runtime with in-process state.

    **Async / Lambda:** ``pending_wake_events`` lists payloads you can drain to
    invoke another API message against ``worker_session_id`` (detached worker).
    Optional ``on_signal`` / ``on_message`` callbacks mirror delivery into the
    parent session (e.g. append a session event or enqueue WebSocket).
    """

    def __init__(
        self,
        parent_agent_name: str = "main_agent",
        on_signal: Callable[[SubAgentSignal], None] | None = None,
        on_message: Callable[[SubAgentMessage], None] | None = None,
    ) -> None:
        self._parent_agent_name = parent_agent_name
        self._on_signal = on_signal
        self._on_message = on_message
        self._bindings: dict[str, WorkerSessionBinding] = {}
        self._parent_for_worker: dict[str, str] = {}
        self._messages: list[SubAgentMessage] = []
        self._thread_to_worker: dict[str, str] = {}
        self.pending_wake_events: list[dict[str, Any]] = []

    def spawn_subagent(
        self,
        parent_session_id: str,
        agent_name: str,
        initial_message: str,
        task_id: Optional[str] = None,
        mode: Literal["background_worker", "thread_bound"] = "background_worker",
        metadata: Optional[dict[str, Any]] = None,
    ) -> WorkerSessionBinding:
        et, eid, _pt = parse_session_key(parent_session_id)
        worker_thread = str(uuid.uuid4())
        worker_session_id = format_session_key(et, eid, worker_thread)
        binding = WorkerSessionBinding(
            parent_session_id=parent_session_id,
            worker_session_id=worker_session_id,
            worker_agent_name=agent_name,
            task_id=task_id,
            mode=mode,
            status="active",
            thread_id=worker_thread,
            metadata=dict(metadata or {}),
        )
        self._bindings[worker_session_id] = binding
        self._parent_for_worker[worker_session_id] = parent_session_id
        self.pending_wake_events.append(
            {
                "kind": "worker_start",
                "parent_session_id": parent_session_id,
                "worker_session_id": worker_session_id,
                "agent_name": agent_name,
                "initial_message": initial_message,
                "task_id": task_id,
                "mode": mode,
            }
        )
        self.send_message_to_worker(
            parent_session_id,
            worker_session_id,
            initial_message,
            task_id=task_id,
            metadata={"spawn": True},
        )
        return binding

    def send_message_to_worker(
        self,
        parent_session_id: str,
        worker_session_id: str,
        content: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentMessage:
        mid = str(uuid.uuid4())
        wbind = self._bindings.get(worker_session_id)
        target_name = wbind.worker_agent_name if wbind else "worker"
        msg = SubAgentMessage(
            message_id=mid,
            source_session_id=parent_session_id,
            target_session_id=worker_session_id,
            source_agent_name=self._parent_agent_name,
            target_agent_name=target_name,
            content=content,
            direction="parent_to_worker",
            task_id=task_id,
            metadata=dict(metadata or {}),
            created_at=datetime.utcnow(),
        )
        self._messages.append(msg)
        if self._on_message:
            self._on_message(msg)
        return msg

    def send_message_to_parent(
        self,
        worker_session_id: str,
        parent_session_id: str,
        content: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentMessage:
        wn = self._bindings.get(worker_session_id)
        agent_name = wn.worker_agent_name if wn else "worker"
        mid = str(uuid.uuid4())
        msg = SubAgentMessage(
            message_id=mid,
            source_session_id=worker_session_id,
            target_session_id=parent_session_id,
            source_agent_name=agent_name,
            target_agent_name=self._parent_agent_name,
            content=content,
            direction="worker_to_parent",
            task_id=task_id,
            metadata=dict(metadata or {}),
            created_at=datetime.utcnow(),
        )
        self._messages.append(msg)
        if self._on_message:
            self._on_message(msg)
        return msg

    def emit_signal(self, signal: SubAgentSignal) -> None:
        if self._on_signal:
            self._on_signal(signal)

    def request_clarification_from_parent(
        self,
        worker_session_id: str,
        parent_session_id: str,
        question: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        wn = self._bindings.get(worker_session_id)
        agent_name = wn.worker_agent_name if wn else "worker"
        sig = SubAgentSignal(
            signal_id=str(uuid.uuid4()),
            signal_type="clarification_needed",
            source_session_id=worker_session_id,
            target_session_id=parent_session_id,
            source_agent_name=agent_name,
            task_id=task_id,
            payload={"question": question, **(metadata or {})},
            timestamp=datetime.utcnow(),
        )
        self.emit_signal(sig)
        return sig

    def report_progress(
        self,
        worker_session_id: str,
        parent_session_id: str,
        update_message: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        return self._emit_typed(
            "progress_update",
            worker_session_id,
            parent_session_id,
            update_message,
            task_id,
            metadata,
        )

    def report_blocked(
        self,
        worker_session_id: str,
        parent_session_id: str,
        reason: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        return self._emit_typed(
            "blocked",
            worker_session_id,
            parent_session_id,
            reason,
            task_id,
            metadata,
        )

    def report_waiting_on_external_party(
        self,
        worker_session_id: str,
        parent_session_id: str,
        update_message: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        return self._emit_typed(
            "waiting_on_external_party",
            worker_session_id,
            parent_session_id,
            update_message,
            task_id,
            metadata,
        )

    def complete_task(
        self,
        worker_session_id: str,
        parent_session_id: str,
        result_message: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        sig = self._emit_typed(
            "task_complete",
            worker_session_id,
            parent_session_id,
            result_message,
            task_id,
            metadata,
        )
        if worker_session_id in self._bindings:
            self._bindings[worker_session_id].status = "completed"
        return sig

    def fail_task(
        self,
        worker_session_id: str,
        parent_session_id: str,
        failure_message: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        sig = self._emit_typed(
            "failure",
            worker_session_id,
            parent_session_id,
            failure_message,
            task_id,
            metadata,
        )
        if worker_session_id in self._bindings:
            self._bindings[worker_session_id].status = "failed"
        return sig

    def cancel_worker(
        self,
        parent_session_id: str,
        worker_session_id: str,
        reason: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        wn = self._bindings.get(worker_session_id)
        worker_name = wn.worker_agent_name if wn else "worker"
        sig = SubAgentSignal(
            signal_id=str(uuid.uuid4()),
            signal_type="cancel",
            source_session_id=parent_session_id,
            target_session_id=worker_session_id,
            source_agent_name=self._parent_agent_name,
            task_id=task_id,
            payload={"message": reason, "worker_agent": worker_name, **(metadata or {})},
            timestamp=datetime.utcnow(),
        )
        self.emit_signal(sig)
        if worker_session_id in self._bindings:
            self._bindings[worker_session_id].status = "canceled"
        return sig

    def _emit_typed(
        self,
        kind: Any,
        worker_session_id: str,
        parent_session_id: str,
        body: str,
        task_id: Optional[str],
        metadata: Optional[dict[str, Any]],
    ) -> SubAgentSignal:
        wn = self._bindings.get(worker_session_id)
        agent_name = wn.worker_agent_name if wn else "worker"
        sig = SubAgentSignal(
            signal_id=str(uuid.uuid4()),
            signal_type=kind,
            source_session_id=worker_session_id,
            target_session_id=parent_session_id,
            source_agent_name=agent_name,
            task_id=task_id,
            payload={"message": body, **(metadata or {})},
            timestamp=datetime.utcnow(),
        )
        self.emit_signal(sig)
        return sig

    def get_active_workers(
        self,
        parent_session_id: str,
        task_id: Optional[str] = None,
    ) -> list[WorkerSessionBinding]:
        out: list[WorkerSessionBinding] = []
        for b in self._bindings.values():
            if b.parent_session_id != parent_session_id:
                continue
            if task_id and b.task_id != task_id:
                continue
            if b.status in ("active", "waiting"):
                out.append(b)
        return out

    def get_conversation_history(
        self,
        parent_session_id: str,
        worker_session_id: str,
        limit: Optional[int] = None,
    ) -> list[SubAgentMessage]:
        rows = [
            m
            for m in self._messages
            if {m.source_session_id, m.target_session_id}
            == {parent_session_id, worker_session_id}
        ]
        rows.sort(key=lambda m: m.created_at or datetime.min)
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def route_thread_message(
        self,
        thread_id: str,
        message_payload: dict[str, Any],
    ) -> Optional[str]:
        del message_payload
        return self._thread_to_worker.get(thread_id)

    def bind_thread_to_worker(self, thread_id: str, worker_session_id: str) -> None:
        self._thread_to_worker[thread_id] = worker_session_id

    def release_thread_binding(self, thread_id: str) -> bool:
        if thread_id in self._thread_to_worker:
            del self._thread_to_worker[thread_id]
            return True
        return False

    def handle_worker_completion(
        self,
        worker_session_id: str,
        result_message: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentSignal:
        parent = self._parent_for_worker.get(worker_session_id)
        if not parent:
            raise ValueError("Unknown worker session")
        return self.complete_task(worker_session_id, parent, result_message, task_id, metadata)

    def handle_worker_message(
        self,
        worker_session_id: str,
        content: str,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SubAgentMessage:
        parent = self._parent_for_worker.get(worker_session_id)
        if not parent:
            raise ValueError("Unknown worker session")
        return self.send_message_to_parent(worker_session_id, parent, content, task_id, metadata)
