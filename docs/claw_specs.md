
OpenClaw-Style Agent Runtime Specs

## 1. Context Engine

Purpose

The Context Engine is responsible for building the final model input for a given agent turn. It does not decide what actions to take, and it does not write memory. Its role is to assemble a bounded, relevant working context from multiple persistent layers.

Responsibilities

The Context Engine should:
	•	load static workspace context
	•	load relevant session history
	•	load relevant memory artifacts
	•	load relevant tool schemas
	•	load runtime metadata
	•	prune or compact old context
	•	produce the final prompt payload for the LLM

Inputs

The Context Engine receives:
	•	current user message or runtime-triggered event
	•	session id
	•	agent id
	•	current task state
	•	session transcript/events
	•	memory store
	•	workspace files
	•	available tools
	•	optional subagent or internal announce messages

Outputs

The Context Engine returns:
	•	ordered prompt messages for the LLM
	•	selected tool definitions
	•	optional context diagnostics/metadata

Context Sources

The engine should support these layers:
	1.	Identity layer
	•	system role
	•	policy
	•	tone/behavior
	2.	Tool layer
	•	tool definitions
	•	tool usage hints
	•	subagent registry
	3.	Session layer
	•	recent user/assistant turns
	•	relevant tool outputs
	•	subagent completion events
	•	task state summary
	4.	Memory layer
	•	stable beliefs
	•	recent journal notes
	•	retrieved semantic notes
	5.	Runtime layer
	•	active task id
	•	workflow state
	•	pending branches
	•	awaiting approvals

Behavior

The default Context Engine should be programmatic, not LLM-driven.

It should:
	•	always include core identity/policy context
	•	include only relevant recent history
	•	include only relevant tools
	•	include only the latest relevant tool results
	•	retrieve memory selectively
	•	avoid blindly appending full session history

Non-Goals

The Context Engine should not:
	•	decide user intent
	•	choose tool calls
	•	write memory
	•	mutate domain state
	•	execute business logic

Extension Points

Custom Context Engines may:
	•	use retrieval over the ledger
	•	use vector search over memory/journal
	•	summarize history dynamically
	•	apply domain-specific routing heuristics
	•	perform schema-aware context loading

⸻

## 2. ReAct Loop

Purpose

The ReAct Loop is the main reasoning/execution cycle of the agent. It receives the context assembled by the Context Engine, calls the LLM, interprets the result, executes tools, persists outputs, and decides whether another reasoning step is needed.

Core Principle

The loop is serialized per session.

There is one active execution loop per session at a time.

High-Level Flow
	1.	receive event
	2.	lock session
	3.	build context
	4.	call LLM
	5.	interpret LLM output
	6.	execute tool calls
	7.	persist events/results
	8.	determine whether another reasoning step is needed
	9.	repeat or terminate

Supported Outputs from the LLM

The LLM may produce one or more of:
	•	user-facing assistant message
	•	one or more tool calls
	•	one or more memory-write side effects
	•	one or more subagent spawns
	•	workflow-state updates
	•	no-op / terminate

Tool Execution Model

Normal tools should be treated as session-blocking for the current step.

This means:
	•	the session waits for the tool result
	•	the result is persisted
	•	the loop continues afterward

Multiple Tools

The runtime may allow the LLM to emit multiple tool calls in one step, but execution policy should distinguish between:
	•	parallelizable independent calls
	•	sequential dependent calls

By default, the runtime should prefer sequential execution unless the plan explicitly marks a set of calls as parallel-safe.

Interpretation Boundary

Tool results should normally be brought back to interpretation:
	•	after each sequential dependency boundary, or
	•	after all required parallel branches are complete

The runtime should avoid premature interpretation when downstream meaning depends on multiple results.

Recommended Execution Policy

Use an explicit structure like:


```
{
  "execution_mode": "sequential|parallel",
  "barrier": true,
  "required_results": ["trips", "expenses", "parking"]
}
```


So the runtime knows when the next LLM interpretation is safe.

Side Effects

Memory writes are not primary work. They are side-effect tool calls and may be executed in the same step as other actions.

Termination

A loop iteration ends when:
	•	the LLM produces a final user-facing response and no more actions are needed
	•	the runtime is awaiting user input
	•	control is transferred to a thread-bound subagent
	•	a background subagent is spawned and no further action is needed now

⸻

## 3. Memory (Belief System)

Purpose

The Belief Memory stores durable semantic facts that remain useful beyond the current task or session.

This is the replacement for MEMORY.md.

Mental Model

Belief Memory is the agent’s long-term semantic model of the world.

It should contain facts like:
	•	identity bindings
	•	user preferences
	•	business rules
	•	stable relationships
	•	reusable conclusions
	•	recurring patterns

Examples
	•	Ricardo’s boss is Maria Chen
	•	Maria prefers Marriott hotels
	•	Trips over $400 require approval
	•	User usually travels from JFK

What Should Be Stored

Promote facts that are:
	•	likely to remain true
	•	useful in future tasks
	•	reusable across sessions
	•	semantically compact
	•	safe to rely on later

What Should Not Be Stored

Do not store:
	•	raw tool results
	•	temporary workflow state
	•	unresolved ambiguity
	•	speculative inferences
	•	large data payloads
	•	one-time event logs

Write Mechanism

Belief Memory is written by the agent loop, through a memory tool or memory adapter.

The Context Engine does not write it.

Promotion may happen:
	•	explicitly during a normal turn
	•	during a pre-compaction memory flush
	•	during explicit user requests to remember something

Storage Model

Recommended structure:
	•	key or document id
	•	semantic content
	•	confidence
	•	source event references
	•	timestamps
	•	tags / entity references

Retrieval

Belief retrieval should support:
	•	direct lookup
	•	semantic search
	•	entity-scoped filtering
	•	task-scoped retrieval

Design Principle

Belief Memory should behave like a belief layer, not a ledger.

⸻

## 4. Memory (Journal)

Purpose

The Journal stores chronological observations, activity traces, and intermediate summaries that are useful for recent recall but are not durable beliefs.

This is the replacement for memory/YYYY-MM-DD.md.

Mental Model

Journal Memory is the agent’s working diary.

It records what happened, not what is permanently true.

Examples
	•	Started planning London trip
	•	Compared Hilton and Marriott
	•	Awaiting approval from finance
	•	Loaded traveler profile for Maria Chen

What Should Be Stored

Store:
	•	relevant daily activity
	•	important task transitions
	•	summarized tool outcomes
	•	subagent milestones
	•	meaningful user interactions

What Should Not Be Stored

Do not store:
	•	every raw event
	•	every token stream
	•	giant payload dumps
	•	full copies of business objects

Write Mechanism

The Journal is also written by the agent loop through the memory subsystem.

Typical triggers:
	•	meaningful workflow transitions
	•	tool completions worth preserving
	•	subagent results
	•	daily task summaries
	•	pre-compaction summarization

Retention Behavior

Journal notes are more transient than beliefs.

They should remain searchable for recent context, but older journal entries may decay in retrieval priority.

Retrieval

Journal retrieval should support:
	•	direct read of today / yesterday
	•	recency-weighted search over older entries
	•	entity-based lookup
	•	task-based lookup

Design Principle

Journal Memory is episodic memory, not semantic memory.

⸻

## 5. SubAgents Runtime

Purpose

The SubAgents Runtime enables the main agent to delegate work to specialized agents running in separate sessions.

These specialized agents are not only execution workers, but also reasoning partners that can communicate with the main agent in natural language.

This allows the main agent to:
	•	delegate complex work
	•	negotiate requirements
	•	ask follow-up questions
	•	receive clarifications
	•	refine outputs iteratively
	•	interact with external agents or chatbots through the same natural-language interface

⸻

Mental Model

A subagent is not just a function call. It is a separate agent session with:
	•	its own context
	•	its own loop
	•	its own transcript
	•	its own tools
	•	its own state

A subagent should be treated as an independent specialist that the main agent can collaborate with asynchronously.

The main agent and the subagent communicate through natural language messages. Natural language acts as the universal contract between agents.

This enables a subagent to:
	•	collaborate with another internal agent
	•	interact with an external chatbot
	•	communicate with systems that do not expose rigid APIs
	•	work across heterogeneous environments without requiring a shared schema for every interaction

⸻

Invocation

Subagents are invoked through a tool-like interface, but they should be treated as session spawns, not normal synchronous tools.

Spawning a subagent creates a new session and establishes a parent-child relationship between:
	•	the requester session
	•	the worker session

The parent agent may then exchange one or more natural-language messages with the worker session over time.

⸻

Modes

A. Background Worker mode
	•	non-blocking
	•	child session runs asynchronously
	•	child session may exchange natural-language messages with the parent session
	•	main session continues running
	•	parent may choose to:
	•	wait for the worker
	•	continue other work
	•	respond to the user while the worker keeps running
	•	worker may send intermediate updates, questions, clarifications, or final results back to the parent

This is the default specialist mode.

The worker is not expected to directly converse with the end user. Instead, it collaborates with the main agent, and the main agent decides what to communicate to the user.

B. Thread-bound persistent mode
	•	conversation ownership moves to the subagent for a given thread
	•	future user messages in that thread route to the subagent session
	•	parent agent steps aside for that thread

This mode is optional and should be used only when direct user-to-specialist interaction is truly desired.

⸻

Natural Language Contract

The runtime should treat natural language as the default communication contract between agents.

This means:
	•	the parent may send goals, clarifications, corrections, and constraints in natural language
	•	the worker may return progress updates, questions, reasoning summaries, recommendations, and final outputs in natural language
	•	structured payloads may still be supported, but they are not required as the primary inter-agent protocol

Examples:
	•	“Please find 3 hotel options in Midtown under $350/night.”
	•	“The user prefers Marriott if price difference is small.”
	•	“I found 5 options, but 2 exceed policy. Should I keep them in the comparison?”
	•	“I was unable to confirm baggage policy through the airline chatbot.”

The use of natural language allows the same worker runtime to communicate with:
	•	internal specialist agents
	•	external AI agents
	•	external vendor chatbots
	•	systems where structured APIs are unavailable or incomplete

⸻

Signals

The runtime should support these signal categories:
	•	task complete
	•	clarification needed
	•	approval needed
	•	failure
	•	escalation
	•	out-of-domain
	•	return-to-parent
	•	cancel
	•	progress update
	•	waiting on external party
	•	blocked

Signals may be represented as explicit structured envelopes, but the payload itself may contain natural-language content.

⸻

Signal Handling

Signals are not required to be a separate built-in protocol object, but the runtime should support structured signal envelopes.

Recommended signal fields:
	•	signal type
	•	source session id
	•	source agent id
	•	target session id
	•	task id
	•	payload
	•	timestamp

The payload may contain:
	•	natural-language content
	•	optional structured metadata
	•	references to artifacts, tools, or domain objects

Example signal payloads:
	•	“I found 6 flights. Two are good candidates, but I need the traveler’s seating preference.”
	•	“The airline chatbot rejected the date format. Please confirm whether I should retry with local timezone formatting.”
	•	“Task complete. Here are the best 3 hotel options.”

⸻

Async Behavior

Background worker subagents should run independently of the parent session loop.

The parent should not block waiting for completion unless explicitly configured.

The parent may:
	•	continue interacting with the user
	•	launch other workers
	•	perform other tool calls
	•	return later to process worker messages

The worker may:
	•	send updates while still running
	•	ask follow-up questions of the parent
	•	wait for the parent’s answer
	•	resume work after receiving new instructions

This means the relationship is not just:
	•	spawn → finish → return

It may instead be:
	•	spawn
	•	exchange messages
	•	refine task
	•	continue execution
	•	return partial outputs
	•	request clarification
	•	complete later

⸻

Parent–Worker Conversation Model

The runtime should support a session-to-session natural-language dialogue between parent and worker.

This dialogue should behave like an internal conversation between agents.

The parent may:
	•	ask the worker to do work
	•	clarify requirements
	•	correct misunderstandings
	•	answer worker questions
	•	request reformulation or deeper analysis
	•	instruct the worker to stop or retry

The worker may:
	•	ask for missing information
	•	explain blockers
	•	propose options
	•	request permission to continue
	•	report uncertainty
	•	return a final recommendation

The worker should not assume that all messages from the parent are raw tool instructions. They may be natural-language guidance.

⸻

Result Return

When a worker produces an output, the runtime should not directly post raw worker output to the user.

Instead:
	1.	worker output is sent back to the requester session as internal context
	2.	requester session receives it as an inter-agent message
	3.	requester agent decides whether to:
	•	summarize it
	•	paraphrase it
	•	surface raw parts of it
	•	ask follow-up questions to the worker
	•	negotiate missing details with the worker
	•	call more tools
	•	spawn another worker

The parent remains the primary conversational interface to the user unless direct handoff mode is explicitly chosen.

⸻

Raw vs Paraphrased Output

The main agent should control whether worker output is:
	•	paraphrased
	•	summarized
	•	translated into user-facing language
	•	forwarded raw

Default behavior:
	•	clarification questions from the worker should usually be paraphrased by the main agent
	•	internal negotiation should remain hidden from the user
	•	machine-oriented or specialist-oriented exchanges should stay between agents
	•	user-relevant artifacts may be surfaced raw when appropriate

Examples of outputs that may be surfaced raw:
	•	hotel option tables
	•	flight result lists
	•	policy comparison tables
	•	chatbot transcript excerpts when useful

Examples of outputs that should usually be paraphrased:
	•	negotiation between parent and worker
	•	internal strategy discussion
	•	retry requests
	•	ambiguity resolution
	•	domain-specific worker uncertainty

⸻

External Agent / Chatbot Compatibility

A worker may communicate not only with internal agents but also with external conversational systems.

Examples:
	•	airline chatbot
	•	hotel booking chatbot
	•	vendor support assistant
	•	partner-company agent

Because natural language is the default contract, the worker runtime can interact with these systems without requiring that they implement the same schema or runtime protocol.

In this model, the worker behaves as a conversational specialist adapter.

⸻

Domain Exit

For background workers, unrelated user questions are handled by the main agent, because the worker does not own the user conversation.

This is one of the main advantages of the background worker model:
	•	the worker stays focused
	•	the main agent absorbs tangents
	•	the user experience stays coherent

For thread-bound subagents, the runtime should still support strategies for unrelated user questions:
	•	let specialist answer anyway
	•	specialist escalates back to parent
	•	router reassigns thread to main agent

⸻

Design Principle

Subagents are independent specialists connected by async session-to-session signaling and natural-language collaboration.

The default specialist model should be:
	•	separate session
	•	async execution
	•	natural-language parent/worker dialogue
	•	parent remains user-facing
	•	worker remains task-focused

This keeps specialists narrow, reusable, and capable of interacting with both internal and external conversational agents.

⸻

## 6. Sessions

Purpose

A Session is the durable execution context for an agent.

It is not just a chat transcript.

Mental Model

Session = event ledger + execution context

Session Contents

A session may contain:
	•	user messages
	•	assistant messages
	•	tool calls
	•	tool results
	•	memory writes
	•	subagent spawns
	•	subagent announce-backs
	•	workflow state changes
	•	compaction summaries
	•	system/runtime events

Lifetime

Sessions are intended to be long-lived.

They may persist for:
	•	days
	•	weeks
	•	months

A session may outlive UI activity.

Session vs Chat
	•	chat = transport/UI stream
	•	session = execution context

A chat event is routed into a session.
A session emits messages back into the chat.

Session Identity

A session should be deterministically derived from routing information such as:
	•	agent id
	•	channel
	•	account
	•	peer
	•	thread

Session Storage

Recommended design:
	•	append-only event documents
	•	optional session metadata record
	•	optional task state record
	•	optional compaction records

Session Tree

Sessions may spawn child sessions.

So runtime state is not only a single timeline, but potentially a session tree:
	•	parent session
	•	child subagent sessions
	•	nested worker sessions

Design Principle

The session is the authoritative execution ledger, not the prompt.

⸻

## 7. Compaction

Purpose

Compaction prevents long-lived sessions from overflowing model context while preserving useful continuity.

Core Principle

The runtime should distinguish between:
	•	stored ledger
	•	working context

Compaction reduces working context size without discarding important meaning.

What Compaction Does

Compaction should:
	•	summarize older parts of the session
	•	preserve important conclusions
	•	preserve task-relevant facts
	•	preserve links to important objects/events
	•	reduce raw history included in future prompts

What Compaction Should Not Do

Compaction should not:
	•	delete authoritative world-changing events without trace
	•	remove task state still needed for execution
	•	discard durable beliefs without promoting them first

Trigger Conditions

Compaction may be triggered by:
	•	token budget threshold
	•	natural workflow boundary
	•	session age
	•	idle boundary
	•	manual instruction

Pre-Compaction Memory Flush

Before compaction, the runtime should perform a memory flush step:
	•	extract durable beliefs
	•	write belief memory
	•	write relevant journal summaries
	•	preserve critical task state

Compaction Outputs

A compaction pass should produce:
	•	compact summary artifact
	•	references to covered ledger ranges
	•	promoted belief updates
	•	promoted journal entries if needed

Retained Hot Context

After compaction, the Context Engine should still keep:
	•	recent turns
	•	active task state
	•	unresolved questions
	•	latest relevant tool results
	•	latest relevant subagent signals

Recommended Layers

Use three layers:
	1.	Cold ledger
	•	full event history
	•	raw payloads
	•	audit trail
	2.	Warm summaries
	•	compaction summaries
	•	journal summaries
	•	promoted conclusions
	3.	Hot context
	•	active workflow state
	•	latest relevant turns
	•	current pending decisions

Design Principle

Compaction is not deletion. It is semantic compression.

⸻

## Cross-Cutting Design Rules

Rule 1: Ledger is not context

Do not treat the full session ledger as the model prompt.

Rule 2: Memory is not the ledger

Belief and journal memory should be distilled from the ledger.

Rule 3: Task state should be explicit

Do not rely only on transcript scanning to know what is next.

Maintain structured task state.

Rule 4: Async work should use sub-sessions

If a workflow is slow or parallelizable, prefer subagents over blocking the main session.

Rule 5: Retrieval should be selective

Load only what is relevant to the current decision.