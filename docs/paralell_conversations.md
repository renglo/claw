Managing Parallel Subagent Conversations in a Triage Agent

Core Idea: Treat Subagent Calls as Workstreams

Instead of:

triage → subagent → result → triage

Use:

triage
 ├── workstream: quote_trip_friend_A
 ├── workstream: quote_trip_friend_B
 ├── workstream: catering
 └── workstream: reservations

Each workstream:

* has its own session_id
* has its own state
* progresses asynchronously
* can request input from the user
* reports milestones back to triage

So triage becomes a conversation scheduler + dependency resolver.

⸻

Minimal Data Model

Store something like this in triage workspace:

active_workstreams = {
    "trip_friend_A": {
        "agent": "trip_quote_agent",
        "session_id": "...",
        "status": "waiting_for_user",
        "pending_question": "Which airport should they depart from?",
        "priority": 2
    },
    "trip_friend_B": {
        "agent": "trip_quote_agent",
        "session_id": "...",
        "status": "running",
    },
    "catering": {
        "agent": "catering_agent",
        "session_id": "...",
        "status": "blocked",
        "depends_on": ["reservation_size"]
    }
}

Now the triage agent is tracking state of conversations, not content of conversations.

⸻

Strategy 1: Conversation Multiplexing

When the user answers:

“He can fly from Madrid”

Triage must decide:

Which workstream does this belong to?

Example:

for stream in active_workstreams:
    if stream.status == "waiting_for_user":
        check relevance

Two implementation options:

Option A — LLM Classification

Prompt:

Which workstream does this message belong to?
Active workstreams:
1. Trip quote for friend from Europe
2. Trip quote for friend from South America
3. Catering planning
User message:
"He can fly from Madrid"

Return:

trip_friend_A

Simple and robust.

⸻

Option B — Deterministic Routing Using Continuity Tokens

Example:

[#trip_friend_A] He can fly from Madrid

Even better if subagents inject tags automatically.

⸻

Strategy 2: Obligation Queue (Very Powerful)

Maintain a queue like:

pending_obligations = [
    ("trip_friend_A", "awaiting_departure_city"),
    ("trip_friend_B", "awaiting_budget_confirmation"),
]

When user replies:

He can fly from Madrid

Resolve:

matches obligation: trip_friend_A

This avoids ambiguity and reduces LLM calls.

⸻

Strategy 3: Subagents Should Speak Through Contracts

Instead of returning raw text like:

Where is your friend flying from?

Return structured intent:

{
  "type": "question",
  "target": "user",
  "field": "departure_city",
  "workstream": "trip_friend_A"
}

Now triage becomes a mediator with semantic clarity.

This matches the natural language as universal contract philosophy while still enabling machine routing.

⸻

Strategy 4: Treat Workstreams as Mini State Machines

Each workstream:

INIT
→ COLLECT_PARAMS
→ PRESENT_OPTIONS
→ WAIT_DECISION
→ BOOK
→ DONE

Example:

trip_friend_A.state = WAIT_DECISION
trip_friend_B.state = COLLECT_PARAMS

Now triage always knows:

* what is missing
* what is blocked
* what can proceed
* what depends on something else

This aligns naturally with BDI-style flat state updates.

⸻

Strategy 5: Let Subagents Run in Background Mode

Execution model:

spawn_subagent(session_id="trip_friend_A")
continue
spawn_subagent(session_id="trip_friend_B")

Later:

trip_friend_A → emits question
trip_friend_B → emits itinerary options

Triage receives events:

EVENT: question_required
EVENT: options_ready
EVENT: booking_confirmed

So triage becomes event-driven instead of call-driven.

⸻

Strategy 6: Maintain a Conversation Agenda

Think of triage holding:

agenda = [
    ask_user("departure_city_friend_A"),
    confirm("budget_friend_B"),
    select("restaurant_option"),
]

Then each user reply pops one agenda item.

This is extremely stable UX-wise and prevents cross-talk between threads.

⸻

Strategy 7: Present Parallel Threads Explicitly to the User

Example:

Here’s where we are:
✈️ Friend from Europe
Waiting for departure airport
✈️ Friend from South America
I have 3 flight options ready
🍽 Catering
Waiting for guest count

Then user can respond naturally:

Europe friend leaves from Madrid

This dramatically improves alignment.

⸻

Recommended Hybrid Model (Best Fit for Handler-Based Agent Architectures)

Use:

* Workstream registry
* Obligation queue
* Event-driven subagent runtime
* State machine per workstream

So triage loop becomes:

1. receive user message
2. resolve target workstream
3. forward to subagent(session_id)
4. receive structured response
5. update workspace state
6. update obligations
7. decide next best question

Instead of:

interpret everything every turn

This keeps triage lightweight even with 5–10 concurrent threads and scales naturally to multi-domain orchestration scenarios like trip planning, catering coordination, and venue reservations.