from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional


# ============================================================
# Core Data Models
# ============================================================


@dataclass
class SessionEvent:
    """
    Represents one durable event in a session ledger.

    Examples:
    - user message received
    - assistant message emitted
    - tool call requested
    - tool result received
    - memory write performed
    - subagent spawned
    - subagent signal received
    - compaction summary created

    ``metadata`` is optional extra data persisted under ``_meta`` on the turn document row
    (alongside ``event_id``, ``session_id``, ``timestamp``).
    """
    event_id: str
    session_id: str
    event_type: str
    timestamp: datetime
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptMessage:
    """
    Represents one message that will be sent to the LLM as part of the
    assembled prompt.
    """
    role: Literal["system", "user", "assistant", "tool", "internal"]
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDefinition:
    """
    Represents a tool that the LLM may call.

    Scheduler routing (extension / handler / tool_init) lives in ``metadata`` when
    loaded from ``schd_tools`` (see ``Loop._load_tool_definitions``).
    """
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """
    Represents a tool call requested by the LLM.
    """
    tool_name: str
    arguments: dict[str, Any]
    call_id: Optional[str] = None


@dataclass
class ToolResult:
    """
    Represents the output of a tool execution.

    ``result`` is the handler return value's ``output`` field only (not the full
    ``{success, output, stack, ...}`` envelope).
    """
    tool_name: str
    call_id: Optional[str]
    success: bool
    result: Any
    error: Optional[str] = None


@dataclass
class MemoryFact:
    """
    Represents one long-term belief stored in the belief system.
    """
    fact_id: str
    subject: Optional[str]
    predicate: Optional[str]
    value: Any
    confidence: float
    source_event_ids: list[str]
    updated_at: datetime
    tags: list[str] = field(default_factory=list)


@dataclass
class JournalEntry:
    """
    Represents one episodic / daily journal entry.
    """
    entry_id: str
    journal_date: str
    session_id: str
    summary: str
    source_event_ids: list[str]
    tags: list[str] = field(default_factory=list)
    created_at: Optional[datetime] = None


@dataclass
class TaskState:
    """
    Represents the explicit working state of an active task.

    This is separate from the session ledger. It should contain the minimum
    structured state necessary for the runtime to know what is currently
    happening and what is next.
    """
    task_id: str
    session_id: str
    status: str
    active_step: Optional[str] = None
    required_branches: list[str] = field(default_factory=list)
    completed_branches: list[str] = field(default_factory=list)
    pending_inputs: list[str] = field(default_factory=list)
    references: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextBundle:
    """
    Output of the Context Engine.

    Contains the final prompt messages and the tool definitions that should
    be exposed to the model for a given turn.

    ``response_format`` is optional; when set (e.g. ``{"type": "json_object"}``), adapters
    that support it pass it through to the provider for structured completions.
    """
    messages: list[PromptMessage]
    tools: list[ToolDefinition]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    response_format: Optional[dict[str, Any]] = None


@dataclass
class ReactDecision:
    """
    Represents the interpreted output of one LLM turn.

    A single turn may contain:
    - a user-facing assistant message
    - tool calls
    - belief writes
    - journal writes
    - subagent spawns
    - task state updates
    - loop termination signals
    """
    assistant_message: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    belief_writes: list[dict[str, Any]] = field(default_factory=list)
    journal_writes: list[dict[str, Any]] = field(default_factory=list)
    subagent_requests: list[dict[str, Any]] = field(default_factory=list)
    task_state_patch: Optional[dict[str, Any]] = None
    should_continue: bool = False
    awaiting_user_input: bool = False


@dataclass
class SubAgentRequest:
    """
    Represents an instruction from a parent session to a worker subagent.

    The payload is intentionally flexible because the parent and worker
    communicate primarily through natural language, which acts as the
    universal contract between agents.
    """
    request_id: str
    parent_session_id: str
    worker_session_id: str
    worker_agent_name: str
    task_id: Optional[str]
    mode: Literal["background_worker", "thread_bound"]
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass
class SubAgentMessage:
    """
    Represents one natural-language message exchanged between a parent
    session and a worker session.

    This is the core inter-agent communication object.
    """
    message_id: str
    source_session_id: str
    target_session_id: str
    source_agent_name: str
    target_agent_name: Optional[str]
    content: str
    direction: Literal["parent_to_worker", "worker_to_parent"]
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass
class SubAgentSignal:
    """
    Represents a structured runtime signal exchanged between sessions.

    Signals may wrap or accompany natural-language content, but they also
    expose machine-friendly signal categories that the runtime can act on.
    """
    signal_id: str
    signal_type: Literal[
        "task_complete",
        "clarification_needed",
        "approval_needed",
        "failure",
        "escalation",
        "out_of_domain",
        "return_to_parent",
        "cancel",
        "progress_update",
        "waiting_on_external_party",
        "blocked",
    ]
    source_session_id: str
    target_session_id: str
    source_agent_name: str
    task_id: Optional[str]
    payload: dict[str, Any]
    timestamp: datetime


@dataclass
class WorkerSessionBinding:
    """
    Represents the relationship between a parent session and a worker session.
    """
    parent_session_id: str
    worker_session_id: str
    worker_agent_name: str
    task_id: Optional[str]
    mode: Literal["background_worker", "thread_bound"]
    status: Literal["active", "waiting", "completed", "canceled", "failed"]
    thread_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)



@dataclass
class CompactionResult:
    """
    Represents the result of a compaction pass.
    """
    session_id: str
    compacted_event_ids: list[str]
    summary_event: SessionEvent
    promoted_fact_ids: list[str] = field(default_factory=list)
    promoted_journal_entry_ids: list[str] = field(default_factory=list)


@dataclass
class IncomingEvent:
    """
    Represents an inbound event that may trigger the runtime.

    Examples:
    - user message
    - subagent completion signal
    - scheduled wake-up event
    - internal system event
    """
    event_type: str
    session_id: str
    payload: dict[str, Any]
    timestamp: datetime


# ============================================================
# Supporting Contracts / Services
# ============================================================


class LLMAdapter:
    """
    Contract for the underlying LLM caller.

    This class should be implemented by the Renglo extension using whichever
    LLM provider and prompt format is preferred.
    """

    def complete(self, context: ContextBundle) -> dict[str, Any]:
        """
        Send the assembled prompt and tool definitions to the model.

        Input:
        - context: ContextBundle containing prompt messages and tool definitions

        Output:
        - Raw model output in a provider-specific or normalized dictionary form.
          The React Loop will later interpret it into a ReactDecision.

        Should do:
        - Call the LLM
        - Return raw output including message text, tool calls, metadata, etc.

        Should not do:
        - Execute tools
        - Persist events
        - Modify memory
        """
        raise NotImplementedError


class ToolRegistry:
    """
    Registry of tools available to the runtime.

    This abstraction allows the Context Engine to fetch tool definitions and
    the React Loop to execute tool calls without caring where the tools are
    implemented.
    """

    def list_tools(self, session_id: str, task_state: Optional[TaskState]) -> list[ToolDefinition]:
        """
        Return the set of tool definitions currently available for the
        given session / task context.

        Input:
        - session_id: current session identifier
        - task_state: current structured task state, if any

        Output:
        - List of ToolDefinition objects that may be exposed to the LLM

        Should do:
        - Resolve relevant tools for the current context
        - Optionally filter tools by task, agent role, or runtime state
        """
        raise NotImplementedError

    def execute_tool(self, tool_call: ToolCall) -> ToolResult:
        """
        Execute one tool call.

        Input:
        - tool_call: ToolCall object containing tool name and arguments

        Output:
        - ToolResult

        Should do:
        - Call the actual underlying handler / implementation
        - Normalize its output into a ToolResult
        """
        raise NotImplementedError


class TaskStateStore:
    """
    Storage contract for structured task state.
    """

    def get_task_state(self, session_id: str) -> Optional[TaskState]:
        """
        Return the active task state for the session, if any.

        Input:
        - session_id

        Output:
        - TaskState or None
        """
        raise NotImplementedError

    def save_task_state(self, task_state: TaskState) -> None:
        """
        Persist the full task state.

        Input:
        - task_state

        Output:
        - None
        """
        raise NotImplementedError

    def patch_task_state(self, session_id: str, patch: dict[str, Any]) -> Optional[TaskState]:
        """
        Apply a partial update to the task state for the session.

        Input:
        - session_id
        - patch: partial field updates

        Output:
        - Updated TaskState or None if no active task exists
        """
        raise NotImplementedError


# ============================================================
# Runtime class implementations: see sibling modules
#   context.py, loop.py, beliefs.py, journal.py, sessions.py,
#   subagents.py, compaction.py, gateway.py
# ============================================================
