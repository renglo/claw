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
    PromptMessage,
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
from .workstreams import ForcedWorkstreamRouting, resolve_forced_workstream_routing

_logger = logging.getLogger(__name__)


def _extract_protocol_update(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        proto = result.get("subagent_protocol")
        if isinstance(proto, dict):
            upd = proto.get("update")
            return upd if isinstance(upd, dict) else {}
        msgs = result.get("messages")
        if isinstance(msgs, list):
            for row in msgs:
                if (
                    isinstance(row, dict)
                    and row.get("_interface") == "subagent_protocol"
                    and isinstance((row.get("_out") or {}).get("content"), dict)
                ):
                    content = (row.get("_out") or {}).get("content") or {}
                    upd = content.get("update")
                    return upd if isinstance(upd, dict) else {}
    if isinstance(result, list):
        for row in result:
            if (
                isinstance(row, dict)
                and row.get("_interface") == "subagent_protocol"
                and isinstance((row.get("_out") or {}).get("content"), dict)
            ):
                content = (row.get("_out") or {}).get("content") or {}
                upd = content.get("update")
                return upd if isinstance(upd, dict) else {}
    return {}


def _collect_protocol_messages_for_user(tool_results: list[ToolResult]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for tr in tool_results:
        if not tr.success:
            continue
        upd = _extract_protocol_update(tr.result)
        m = str(upd.get("message_for_user") or "").strip()
        if not m or m in seen:
            continue
        seen.add(m)
        parts.append(m)
    if not parts:
        return ""
    return "\n\n".join(parts)


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

            routing = ForcedWorkstreamRouting([])
            if iteration == 1 and incoming_event.event_type == "user_message" and self._task_store:
                routing = resolve_forced_workstream_routing(
                    task_state, incoming_event, llm=self._llm
                )
                hook = getattr(self._task_store, "after_forced_workstream_routing", None)
                if callable(hook):
                    hook(routing)
                task_state = self._task_store.get_task_state(session_id)
            split_calls: list[ToolCall] = []
            if (
                iteration == 1
                and not routing.tool_calls
                and self._should_try_intent_splitter(incoming_event, task_state)
            ):
                user_text = str(
                    (incoming_event.payload or {}).get("text")
                    or (incoming_event.payload or {}).get("message")
                    or ""
                )
                split_calls = self._tool_calls_from_intent_split(user_text)

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

            if (routing.tool_calls or split_calls) and iteration == 1:
                forced_calls = routing.tool_calls or split_calls
                tc0 = forced_calls[0]
                self._debug_log(
                    "run_turn:forced_workstream_tool",
                    n=iteration,
                    tool=tc0.tool_name,
                    reference_id=(tc0.arguments or {}).get("reference_id"),
                )
                decision = ReactDecision(
                    assistant_message=None,
                    tool_calls=forced_calls,
                    should_continue=True,
                )
            else:
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
            should_await_user = False
            if decision.tool_calls:
                stamp = getattr(self._task_store, "apply_triage_focal_reference_to_tool_calls", None)
                if callable(stamp):
                    stamp(decision.tool_calls)
                tool_results = self.execute_tool_calls(decision.tool_calls, tool_definitions=all_tools)
                summary["tool_results"].extend([asdict(tr) for tr in tool_results])
                for tr in tool_results:
                    upd = _extract_protocol_update(tr.result)
                    if str(upd.get("message_for_user") or "").strip():
                        should_await_user = True
            defer_assistant = False
            relay_final = ""
            if should_await_user:
                decision.awaiting_user_input = True
                relay = _collect_protocol_messages_for_user(tool_results)
                if relay:
                    pre = str(decision.assistant_message or "").strip()
                    defer_assistant = True
                    relay_final = relay
                    decision.assistant_message = pre or None

            self.persist_side_effects(
                session_id, decision, tool_results, defer_assistant_message=defer_assistant
            )

            if defer_assistant and relay_final:
                decision.assistant_message = relay_final
                now2 = datetime.utcnow()
                relay_ev = SessionEvent(
                    event_id=str(uuid.uuid4()),
                    session_id=session_id,
                    event_type="assistant_message",
                    timestamp=now2,
                    payload={"text": relay_final},
                )
                self.save_event(relay_ev)
                self._emit_roll_ws(relay_ev)

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

            if self._task_store:
                task_state = self._task_store.get_task_state(session_id)

        if last_assistant:
            summary["emitted_message"] = last_assistant
        return summary

    def call_model(self, context: ContextBundle) -> dict[str, Any]:
        return self._llm.complete(context)

    def _should_try_intent_splitter(self, incoming_event: IncomingEvent, task_state: Optional[TaskState]) -> bool:
        if incoming_event.event_type != "user_message":
            return False
        refs = task_state.references if task_state else {}
        aw = refs.get("active_workstreams") if isinstance(refs, dict) else None
        if not isinstance(aw, dict):
            return True
        return not any(isinstance(v, dict) and v.get("status") == "waiting_for_user" for v in aw.values())

    def _split_intents(self, user_text: str) -> dict[str, Any]:
        system = (
            "Classify whether the user message contains one intent or multiple distinct intents/tasks.\n"
            "Return strict JSON with keys: mode, intent_requests, confidence.\n"
            '- mode is "single" or "multi". Use "multi" only when the message clearly asks for 2+ separate requests.\n'
            "intent_requests is an array. For each item include: intent_kind, intent_label, intent_message.\n"
            "intent_kind is a short lowercase category (examples: travel_quote, weather_lookup, side_question, other).\n"
            "intent_message must be a concise standalone user-style message for only that intent.\n"
            "If uncertain, keep mode=single and return an empty intent_requests array."
        )
        bundle = ContextBundle(
            messages=[
                PromptMessage(role="system", content=system),
                PromptMessage(role="user", content=user_text),
            ],
            tools=[],
            response_format={"type": "json_object"},
        )
        raw = self.call_model(bundle)
        msg = (raw.get("choices") or [{}])[0].get("message") or {}
        content = str(msg.get("content") or "").strip()
        if not content or content.startswith("LLM error:"):
            return {"mode": "single", "intent_requests": [], "confidence": 0.0}
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                return {"mode": "single", "intent_requests": [], "confidence": 0.0}
            intents = data.get("intent_requests")
            if not isinstance(intents, list):
                intents = []
            clean_intents = [t for t in intents if isinstance(t, dict)]
            mode = str(data.get("mode") or "single").strip().lower()
            if mode not in ("single", "multi"):
                mode = "single"
            conf = data.get("confidence")
            try:
                confidence = float(conf)
            except Exception:
                confidence = 0.0
            return {"mode": mode, "intent_requests": clean_intents, "confidence": confidence}
        except Exception:
            return {"mode": "single", "intent_requests": [], "confidence": 0.0}

    def _tool_calls_from_intent_split(self, user_text: str) -> list[ToolCall]:
        split = self._split_intents(user_text)
        intents = split.get("intent_requests") or []
        if split.get("mode") != "multi" or len(intents) < 2:
            return []
        calls: list[ToolCall] = []
        for idx, intent in enumerate(intents):
            if not isinstance(intent, dict):
                continue
            kind = str(intent.get("intent_kind") or "").strip().lower()
            if kind != "travel_quote":
                # Loop must stay tool-agnostic; only force known safe decomposition here.
                return []
            intent_msg = str(intent.get("intent_message") or "").strip()
            msg = intent_msg or f"Please provide a travel quote for: {json.dumps(intent, default=str)}"
            args: dict[str, Any] = {
                "message": msg,
                "fingerprint_schema": "travel_quote.v1",
                "fingerprint_payload": {
                    "intent_kind": kind,
                    "intent_label": str(intent.get("intent_label") or "").strip(),
                    "intent_message": intent_msg,
                    "origin": str(intent.get("origin") or intent.get("from") or "").strip(),
                    "destination": str(intent.get("destination") or intent.get("to") or "").strip(),
                    "start_date": str(
                        intent.get("start_date") or intent.get("departure_date") or ""
                    ).strip(),
                    "nights": str(intent.get("nights") or "").strip(),
                    "adults": str(intent.get("adults") or "").strip(),
                },
            }
            label = str(intent.get("intent_label") or "").strip()
            if label:
                args["label"] = label
            calls.append(
                ToolCall(
                    tool_name="agent_quotes",
                    arguments=args,
                    call_id=f"forced-intent-split-{idx}-{uuid.uuid4()}",
                )
            )
        return calls

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
                err_raw = out.get("output") if not ok else None
                if err_raw is not None and not isinstance(err_raw, str):
                    try:
                        err_str = json.dumps(err_raw, default=str)
                        if len(err_str) > 2400:
                            err_str = err_str[:2399] + '…'
                    except Exception:
                        err_str = str(err_raw)[:2400]
                else:
                    err_str = str(err_raw) if err_raw else (None if ok else "handler failed")
                results.append(
                    ToolResult(
                        tool_name=tc.tool_name,
                        call_id=tc.call_id,
                        success=ok,
                        result=out.get("output"),
                        error=None if ok else err_str,
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
        defer_assistant_message: bool = False,
    ) -> None:
        now = datetime.utcnow()
        if decision.assistant_message and not defer_assistant_message:
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

        hook = getattr(self._task_store, "after_tool_calls", None) if self._task_store else None
        if callable(hook):
            hook(session_id, decision.tool_calls, tool_results)

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
