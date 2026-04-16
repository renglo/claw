from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Literal, Optional

from .class_prototypes import (
    ContextBundle,
    IncomingEvent,
    LLMAdapter,
    ReactDecision,
    SessionEvent,
    TaskState,
    TaskStateStore,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)
from .context import Context
from .tools import Tools

_logger = logging.getLogger(__name__)


class Loop:
    """
    ReAct-style loop: context → LLM → interpret → tools / memory / optional async workers → persist.

    Wires optional ``SchdController.handler_call`` when ``tool_registry`` is absent.
    """

    def __init__(
        self,
        llm: LLMAdapter,
        context_engine: Context,
        sessions: Any,
        tool_registry: ToolRegistry | None = None,
        task_state_store: TaskStateStore | None = None,
        beliefs: Any | None = None,
        journal: Any | None = None,
        subagents: Any | None = None,
        data_controller: Any | None = None,
        portfolio: str | None = None,
        org: str | None = None,
        schd_controller: Any | None = None,
        max_loop_iterations: int = 25,
        on_stream: Callable[[dict[str, Any]], None] | None = None,
        on_roll_event: Callable[[dict[str, Any]], None] | None = None,
        debug: bool = False,
    ) -> None:
        self._llm = llm
        self._ctx = context_engine
        self._sessions = sessions
        self._tools = tool_registry
        self._task_store = task_state_store
        self._beliefs = beliefs
        self._journal = journal
        self._subagents = subagents
        self._dc = data_controller
        self._portfolio = portfolio
        self._org = org
        self._schd = schd_controller
        self._max_iter = max_loop_iterations
        self._on_stream = on_stream
        self._on_roll_event = on_roll_event
        self._debug = bool(debug)

    def _debug_log(self, stage: str, **data: Any) -> None:
        if not self._debug:
            return
        parts: list[str] = [f"[claw.loop] {stage}"]
        for k, v in data.items():
            s = repr(v)
            if len(s) > 1200:
                s = s[:1200] + "…"
            parts.append(f"{k}={s}")
        line = " | ".join(parts)
        print(line, flush=True)
        _logger.debug(line)

    def print_chat(self, message: dict[str, Any]) -> None:
        """
        Persist one ``claw_stream`` session event (reload parity), then notify ``on_stream`` for WebSocket only.

        Avoid duplicating turn writes in the agent: persistence lives here alongside ``save_event`` elsewhere.
        """
        body = {"channel": "claw_stream", **message}
        try:
            ev = SessionEvent(
                event_id=str(uuid.uuid4()),
                session_id=self._sessions.session_id,
                event_type="claw_stream",
                timestamp=datetime.utcnow(),
                payload={"text": json.dumps(body, default=str)},
            )
            self.save_event(ev)
        except Exception as e:
            _logger.warning("Failed to persist claw_stream: %s", e)
        if self._on_stream:
            self._on_stream(message)

    def _load_tool_definitions(self, session_id: str, task_state: TaskState | None) -> list[ToolDefinition]:
        if self._tools:
            return self._tools.list_tools(session_id, task_state)
        del session_id, task_state
        if not self._dc or not self._portfolio or not self._org:
            return []
        res = self._dc.get_a_b(self._portfolio, self._org, "schd_tools", limit=500)
        if not res.get("success"):
            return []
        return Tools.tool_definitions_from_items(res.get("items", []), shortlist=None)

    def save_event(self, event: SessionEvent) -> None:
        self._sessions.append_event(event)

    def _emit_roll_ws(self, ev: SessionEvent) -> None:
        """Push the same roll row shape as reload (``Sessions._event_to_message``) to the WebSocket."""
        if not self._on_roll_event:
            return
        try:
            row = self._sessions._event_to_message(ev)
            self._on_roll_event(row)
        except Exception as e:
            _logger.warning("Failed to emit roll event to WebSocket: %s", e)

    def run_turn(self, incoming_event: IncomingEvent) -> dict[str, Any]:
        session_id = incoming_event.session_id
        self._debug_log(
            "run_turn:start",
            session_id=session_id,
            event_type=incoming_event.event_type,
            payload_keys=list((incoming_event.payload or {}).keys()),
        )

        summary: dict[str, Any] = {
            "session_id": session_id,
            "emitted_message": None,
            "tool_results": [],
            "spawned_subagents": [],
            "awaiting_user_input": False,
            "terminated": False,
            "iterations": 0,
        }

        had_turn = self._sessions.get_active_turn_id()
        self._debug_log("run_turn:before_create_turn", active_turn_id=had_turn)
        if not had_turn:
            ctx_payload = incoming_event.payload.get("context") or {"public_user": False}
            self._sessions.create_turn(ctx_payload)
            self._debug_log(
                "run_turn:after_create_turn",
                active_turn_id=self._sessions.get_active_turn_id(),
            )
        else:
            self._debug_log("run_turn:skip_create_turn", active_turn_id=had_turn)

        if incoming_event.event_type == "user_message":
            ut = incoming_event.payload.get("text") or incoming_event.payload.get("message") or ""
            user_ev = SessionEvent(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                event_type="user_message",
                timestamp=incoming_event.timestamp,
                payload={"text": ut},
            )
            self.save_event(user_ev)
            self._emit_roll_ws(user_ev)

        task_state = self._task_store.get_task_state(session_id) if self._task_store else None
        self._debug_log("run_turn:task_state", has_task_state=task_state is not None)

        iteration = 0
        last_assistant: str | None = None
        while iteration < self._max_iter:
            iteration += 1
            summary["iterations"] = iteration
            self.print_chat({"stage": "iteration", "n": iteration})
            if self._debug:
                self._debug_log("run_turn:iteration", n=iteration)

            all_events = self._sessions.get_events(session_id, limit=500)
            sel_events = self._ctx.select_session_events(incoming_event, all_events, task_state)

            all_beliefs = self._beliefs.list_facts(limit=300) if self._beliefs else []
            sel_beliefs = self._ctx.select_beliefs(incoming_event, task_state, all_beliefs)

            all_journal = self._journal.list_recent_entries(200) if self._journal else []
            sel_journal = self._ctx.select_journal_entries(incoming_event, task_state, all_journal)

            all_tools = self._load_tool_definitions(session_id, task_state)
            sel_tools = self._ctx.select_tools(incoming_event, task_state, all_tools)

            self._debug_log(
                "run_turn:context_inputs",
                n=iteration,
                all_events=len(all_events),
                sel_events=len(sel_events),
                all_beliefs=len(all_beliefs),
                sel_beliefs=len(sel_beliefs),
                all_journal=len(all_journal),
                sel_journal=len(sel_journal),
                all_tools=len(all_tools),
                sel_tools=len(sel_tools),
                tool_names=[getattr(t, "tool_name", str(t)) for t in sel_tools],
            )

            bundle = self._ctx.build_context(
                incoming_event,
                sel_events,
                task_state,
                sel_beliefs,
                sel_journal,
                sel_tools,
            )
            self._debug_log(
                "run_turn:before_call_model",
                n=iteration,
                prompt_messages=len(bundle.messages),
                tools=len(bundle.tools),
            )
            
            raw = self.call_model(bundle)
            self._debug_log(
                "run_turn:after_call_model",
                n=iteration,
                raw_keys=list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
                first_choice_preview=(
                    str(raw.get("choices", [{}])[0])[:400]
                    if isinstance(raw, dict) and raw.get("choices")
                    else None
                ),
            )
            decision = self.interpret_model_output(raw)

            tool_results: list[ToolResult] = []
            if decision.tool_calls:
                tool_results = self.execute_tool_calls(decision.tool_calls, tool_definitions=all_tools)
                summary["tool_results"].extend([asdict(tr) for tr in tool_results])

            self.persist_side_effects(session_id, decision, tool_results)

            if decision.assistant_message:
                last_assistant = decision.assistant_message
                summary["emitted_message"] = decision.assistant_message

            for req in decision.subagent_requests or []:
                if self._subagents and isinstance(req, dict):
                    bind = self._subagents.spawn_subagent(
                        parent_session_id=session_id,
                        agent_name=str(req.get("agent_name", "worker")),
                        initial_message=str(req.get("message", "")),
                        task_id=req.get("task_id"),
                        mode=req.get("mode", "background_worker"),
                        metadata=req.get("metadata"),
                    )
                    summary["spawned_subagents"].append(
                        {
                            "worker_session_id": bind.worker_session_id,
                            "agent": bind.worker_agent_name,
                        }
                    )

            if decision.awaiting_user_input:
                summary["awaiting_user_input"] = True
                summary["terminated"] = True
                break

            if not self.should_continue_iteration(decision, tool_results, task_state):
                summary["terminated"] = True
                break

        if last_assistant:
            summary["emitted_message"] = last_assistant
        return summary

    def call_model(self, context: ContextBundle) -> dict[str, Any]:
        return self._llm.complete(context)

    def interpret_model_output(self, model_output: dict[str, Any]) -> ReactDecision:
        if "choices" in model_output and model_output["choices"]:
            msg = model_output["choices"][0].get("message") or {}
            content = msg.get("content") or ""
            tool_calls: list[ToolCall] = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {"_raw": raw_args}
                else:
                    args = raw_args if isinstance(raw_args, dict) else {}
                tool_calls.append(
                    ToolCall(
                        tool_name=str(fn.get("name", "")),
                        arguments=args,
                        call_id=tc.get("id"),
                    )
                )
            return ReactDecision(
                assistant_message=content or None,
                tool_calls=tool_calls,
                should_continue=bool(tool_calls),
                awaiting_user_input=False,
            )

        if "content" in model_output or "tool_calls" in model_output:
            tc_list: list[ToolCall] = []
            for tc in model_output.get("tool_calls") or []:
                if isinstance(tc, ToolCall):
                    tc_list.append(tc)
                elif isinstance(tc, dict):
                    tc_list.append(
                        ToolCall(
                            tool_name=str(tc.get("tool_name", tc.get("name", ""))),
                            arguments=tc.get("arguments") or tc.get("args") or {},
                            call_id=tc.get("call_id"),
                        )
                    )
            return ReactDecision(
                assistant_message=model_output.get("content"),
                tool_calls=tc_list,
                belief_writes=list(model_output.get("belief_writes") or []),
                journal_writes=list(model_output.get("journal_writes") or []),
                subagent_requests=list(model_output.get("subagent_requests") or []),
                task_state_patch=model_output.get("task_state_patch"),
                should_continue=bool(tc_list or model_output.get("should_continue")),
                awaiting_user_input=bool(model_output.get("awaiting_user_input")),
            )

        return ReactDecision(assistant_message=str(model_output), should_continue=False)

    def execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        execution_mode: Literal["sequential", "parallel"] = "sequential",
        tool_definitions: list[ToolDefinition] | None = None,
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        if execution_mode == "parallel":
            raise NotImplementedError("Parallel tool execution is not implemented in this runtime.")
        by_name = {td.tool_name: td for td in (tool_definitions or [])}
        for tc in tool_calls:
            defn = by_name.get(tc.tool_name)
            if not defn:
                results.append(
                    ToolResult(
                        tool_name=tc.tool_name,
                        call_id=tc.call_id,
                        success=False,
                        result={},
                        error=f"No tool definition found for '{tc.tool_name}'",
                    )
                )
                continue

            meta = defn.metadata if isinstance(defn.metadata, dict) else {}
            ext_raw = meta.get("extension")
            h_raw = meta.get("handler")
            extension = "" if ext_raw is None else str(ext_raw).strip()
            handler = "" if h_raw is None else str(h_raw).strip()
            if not extension or not handler:
                results.append(
                    ToolResult(
                        tool_name=tc.tool_name,
                        call_id=tc.call_id,
                        success=False,
                        result={},
                        error="Tool definition missing extension or handler in metadata",
                    )
                )
                continue

            params: dict[str, Any] = dict(tc.arguments)
            init = meta.get("tool_init")
            if not isinstance(init, dict):
                init = {}
            params["_init"] = init
            params["_delegated"] = True

            if self._schd and self._portfolio and self._org:
                out = self._schd.handler_call(
                    self._portfolio,
                    self._org,
                    extension,
                    handler,
                    params,
                )
                ok = bool(out.get("success"))
                results.append(
                    ToolResult(
                        tool_name=tc.tool_name,
                        call_id=tc.call_id,
                        success=ok,
                        result=out.get("output"),
                        error=None if ok else str(out.get("output")),
                    )
                )
                continue
            results.append(
                ToolResult(
                    tool_name=tc.tool_name,
                    call_id=tc.call_id,
                    success=False,
                    result={},
                    error="No tool_registry or schd_controller configured",
                )
            )
        return results

    def persist_side_effects(
        self,
        session_id: str,
        decision: ReactDecision,
        tool_results: list[ToolResult],
    ) -> None:
        now = datetime.utcnow()
        if decision.assistant_message:
            asst_ev = SessionEvent(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                event_type="assistant_message",
                timestamp=now,
                payload={"text": decision.assistant_message},
            )
            self.save_event(asst_ev)
            self._emit_roll_ws(asst_ev)

        for tc in decision.tool_calls:
            tc_ev = SessionEvent(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                event_type="tool_call",
                timestamp=now,
                payload={"tool": tc.tool_name, "arguments": tc.arguments, "call_id": tc.call_id},
            )
            self.save_event(tc_ev)
            self._emit_roll_ws(tc_ev)

        for tr in tool_results:
            tr_ev = SessionEvent(
                event_id=str(uuid.uuid4()),
                session_id=session_id,
                event_type="tool_result",
                timestamp=now,
                payload={
                    "tool": tr.tool_name,
                    "call_id": tr.call_id,
                    "success": tr.success,
                    "result": tr.result,
                    "error": tr.error,
                },
            )
            self.save_event(tr_ev)
            self._emit_roll_ws(tr_ev)

        for bw in decision.belief_writes:
            if self._beliefs:
                self._beliefs.write_fact(bw, bw.get("source_event_ids") or [])

        for jw in decision.journal_writes:
            if self._journal:
                self._journal.append_entry(
                    journal_date=str(jw.get("journal_date") or now.date().isoformat()),
                    summary=str(jw.get("summary", "")),
                    session_id=session_id,
                    source_event_ids=list(jw.get("source_event_ids") or []),
                    tags=jw.get("tags"),
                )

        if decision.task_state_patch and self._task_store:
            self._task_store.patch_task_state(session_id, decision.task_state_patch)

    def should_continue_iteration(
        self,
        decision: ReactDecision,
        tool_results: list[ToolResult],
        task_state: Optional[TaskState],
    ) -> bool:
        del task_state
        if decision.awaiting_user_input:
            return False
        if tool_results:
            return True
        if decision.should_continue:
            return True
        return False
