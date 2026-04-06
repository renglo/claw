from __future__ import annotations

from .gateway import Gateway
from .loop import Loop
from .subagents import SubAgents
from .context import Context
from .sessions import Sessions
from .beliefs import Beliefs
from .journal import Journal
from .tools import Tools
from .models import Models

from renglo.data.data_controller import DataController
from renglo.session.session_controller import SessionController
from renglo.schd.schd_controller import SchdController
from renglo.common import load_config
from renglo.agent.websocket_client import WebSocketClient

from .class_prototypes import SessionEvent, SubAgentMessage, SubAgentSignal

import json
import logging
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass
from contextvars import ContextVar

_logger = logging.getLogger(__name__)



@dataclass
class RequestContext:
    """
    Attributes
    ----------
    connection_id : str
        WebSocket connection ID for responding to user
    portfolio : str
        Portfolio ID
    org : str
        Organization ID
    public_user : str
        External user ID (for messages from outside the system)
    entity_type : str
        Entity type (e.g., 'noma_travels')
    entity_id : str
        Entity ID (e.g., trip_id)
    thread : str
        Thread ID
    workspace_id : str
        Workspace ID
    chat_id : str
        Chat ID
    workspace : Dict[str, Any]
        Workspace document with cache and state
    message : str
        User message text
    """
    connection_id: str = ''
    portfolio: str = ''
    org: str = ''
    public_user: str = ''
    entity_type: str = ''
    entity_id: str = ''
    thread: str = ''
    workspace_id: str = ''
    message: str = ''
    

# Create a context variable to store the request context
request_context: ContextVar[RequestContext] = ContextVar('request_context', default=RequestContext())


class GenericAgent:


    def __init__(self) -> None:
        self.config = load_config()
        self.DAC = DataController(config=self.config)
        self.SSC = SessionController(config=self.config)
        self.SHC = SchdController(config=self.config)
        ws_url = str(self.config.get("WEBSOCKET_CONNECTIONS", "") or "")
        self._ws = WebSocketClient(ws_url)
        self._sessions: Optional[Sessions] = None

    def _get_context(self) -> RequestContext:
        return request_context.get()

    def _set_context(self, context: RequestContext) -> None:
        request_context.set(context)

    def _send_ws(self, doc: Dict[str, Any], connection_id: Optional[str] = None) -> bool:
        """
        Push a chat-shaped document to the API Gateway WebSocket client, same contract as
        AgentUtilities.print_chat: post_to_connection(connection_id, payload).
        """
        cid = connection_id or self._get_context().connection_id
        if not cid or not self._ws.is_configured():
            return False
        return self._ws.send_message(cid, doc)

    def _persist_realtime_event(self, event_type: str, body: Dict[str, Any]) -> None:
        """
        Mirror WebSocket payloads on the active turn so reload reads the same rows from session storage.

        Uses the same ``_type`` / string JSON in ``_out.content`` shape as ``_send_ws`` (via Sessions roll encoding).
        """
        ss = self._sessions
        if ss is None:
            return
        try:
            ev = SessionEvent(
                event_id=str(uuid.uuid4()),
                session_id=ss.session_id,
                event_type=event_type,
                timestamp=datetime.utcnow(),
                payload={"text": json.dumps(body, default=str)},
            )
            ss.append_event(ev)
        except Exception as e:
            _logger.warning("Failed to persist realtime event %s: %s", event_type, e)

    def on_signal(self, signal: SubAgentSignal) -> None:
        """
        Structured subagent lifecycle events (progress, blocked, task_complete, failure, …).
        Distinct from conversational text: machine-oriented ``signal_type`` + payload for UI chrome
        (badges, toasts, stepper), not a chat bubble line.
        """
        body = {
            "channel": "claw_signal",
            "signal_type": signal.signal_type,
            "signal_id": signal.signal_id,
            "source_session_id": signal.source_session_id,
            "target_session_id": signal.target_session_id,
            "source_agent": signal.source_agent_name,
            "task_id": signal.task_id,
            "payload": signal.payload,
            "timestamp": signal.timestamp.isoformat() if signal.timestamp else None,
        }
        doc = {
            "_type": "claw_signal",
            "_out": {"role": "assistant", "content": json.dumps(body, default=str)},
        }
        self._persist_realtime_event("claw_signal", body)
        self._send_ws(doc)

    def on_message(self, message: SubAgentMessage) -> None:
        """
        Natural-language relay between parent and worker sessions (inter-agent dialogue).
        Use for transcript-style lines the user may optionally see; not iteration counters.
        """
        body = {
            "channel": "claw_subagent_message",
            "message_id": message.message_id,
            "direction": message.direction,
            "source_session_id": message.source_session_id,
            "target_session_id": message.target_session_id,
            "source_agent": message.source_agent_name,
            "target_agent": message.target_agent_name,
            "task_id": message.task_id,
            "content": message.content,
            "metadata": message.metadata,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }
        doc = {
            "_type": "claw_subagent_message",
            "_out": {"role": "assistant", "content": json.dumps(body, default=str)},
        }
        self._persist_realtime_event("claw_subagent_message", body)
        self._send_ws(doc)

    def on_stream(self, message: Dict[str, Any]) -> None:
        """
        WebSocket delivery for loop status from ``Loop.print_chat``.

        The turn is updated once in ``Loop.print_chat`` (``claw_stream`` via ``save_event``); this callback
        only mirrors the same payload to the client so we do not append the same stream twice.
        """
        body = {"channel": "claw_stream", **message}
        doc = {
            "_type": "claw_stream",
            "_out": {"role": "assistant", "content": json.dumps(body, default=str)},
        }
        self._send_ws(doc)

    def on_roll_event(self, row: Dict[str, Any]) -> None:
        """
        Push persisted roll events (``user_message``, ``assistant_message``, ``tool_call``, ``tool_result``)
        over the WebSocket using the same document shape as ``Sessions._event_to_message`` / reload.
        """
        self._send_ws(row)

        
    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = "run >  agent"
        context = RequestContext()

        if isinstance(payload, str):
            try:
                payload = json.loads(payload) if payload.strip() else {}
            except json.JSONDecodeError:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}

        if "connectionId" in payload:
            context.connection_id = payload["connectionId"]
        if "portfolio" in payload:
            context.portfolio = payload["portfolio"]
        else:
            return {"success": False, "action": action, "input": payload, "output": "No portfolio provided"}
        if "org" in payload:
            context.org = payload["org"]
        else:
            context.org = "_all"
        if "public_user" in payload:
            context.public_user = payload["public_user"]
        if "entity_type" in payload:
            context.entity_type = payload["entity_type"]
        else:
            context.entity_type = "ag1"
        if "entity_id" in payload:
            context.entity_id = payload["entity_id"]
        else:
            context.entity_id = "5678a"
        if "thread" in payload:
            context.thread = payload["thread"]
        else:
            context.thread = "1234c"
        if "workspace" in payload:
            context.workspace_id = payload["workspace"]
        if "data" in payload:
            context.message = payload["data"]


        self._set_context(context)

        ss = Sessions(
            session_controller = self.SSC,
            portfolio = context.portfolio,
            org = context.org,
            entity_type = context.entity_type,
            entity_id = context.entity_id,
            thread_id = context.thread,
            data_controller = self.DAC,
        )
        self._sessions = ss
        
        be = Beliefs(
            data_controller = self.DAC,
            portfolio = context.portfolio,
            org= context.org
        )
        
        jo = Journal(
            data_controller = self.DAC,
            portfolio = context.portfolio,
            org= context.org,
            entity_type = context.entity_type,
            entity_id = context.entity_id
        )
        
        sa = SubAgents(
            parent_agent_name = 'main_agent',
            on_signal = self.on_signal,
            on_message = self.on_message,
        )
        
        cx = Context()
        ll = Models(config=self.config)
        
        tl = Tools(
            data_controller=self.DAC,
            portfolio=context.portfolio,
            org=context.org,
            shortlist=['quote_agent'],
        )
        
        
        lp = Loop(
            llm=ll,
            context_engine=cx,
            sessions=ss,
            tool_registry=tl,
            task_state_store=None,
            beliefs=be,
            journal=jo,
            subagents=sa,
            data_controller=self.DAC,
            portfolio=context.portfolio,
            org=context.org,
            schd_controller=self.SHC,
            max_loop_iterations=25,
            on_stream=self.on_stream,
            on_roll_event=self.on_roll_event,
            debug=True,
        )
        
        
        gw = Gateway(
            loop=lp,
            subagents=sa,
            portfolio=context.portfolio,
            org=context.org,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            default_thread_id='111111'   
        )
        
        try:
            summary = gw.handle_incoming_message(
                agent_name='unouno',
                channel='dosdos',
                payload={'message':context.message},
                account_id='trestres',
                peer_id='cuatrocuatro',
                thread_id=context.thread
            )
            return {
                "success": True,
                "action": action,
                "input": payload,
                "output": summary,
            }
        finally:
            self._sessions = None

