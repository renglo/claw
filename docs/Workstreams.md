Workstreams

The problem it solves
The agent can have several ongoing jobs at once (for example: two trip quotes, or a quote plus something else). Each job needs its own name so the system knows which job a message belongs to and doesn’t mix up state between them.

workstreams.py is the claw layer that names those jobs, remembers their status, tells the LLM about them, and routes the user’s next message when more than one job is waiting for an answer.

The main pieces
1. WorkstreamRegistry (the notebook)
Think of a single shared note attached to the chat thread (the “workspace” document). On that note there is a section called workstreams: a flat list of jobs, each keyed by a string id (reference_id), with things like:

which tool/handler owns it (e.g. agent_quotes),
whether it’s waiting for the user, completed, or errored,
short previews of the last message / last result,
sometimes a trip_id once the trip exists.
The registry loads that section, saves updates after tool calls, and exposes this as “task state” so the rest of the loop can treat “active workstreams” like structured state.

2. Workstreams (the reminder in the model’s prompt)
This is a small context builder. When the model is about to think, it can get an extra internal message that says, in effect:

here are the active workstreams (the JSON snapshot),
here is the triage focal id (default / last-used hex id for this thread),
how to continue a waiting flow vs start something separate.
So the file doesn’t “run the agent”; it injects instructions + data so the model can call tools with the right reference_id.

3. Forced routing (resolve_forced_workstream_routing, etc.)
Sometimes more than one workstream is in “waiting_for_user”. The user sends one new message; the system must guess: Are they answering flow A, flow B, or starting something new?

When that happens (on the first step of a user turn), claw can run a small extra LLM call (the “selector”) that returns either:

continue with a specific reference_id, and then claw forces a tool call to that handler with that id and the user text, or
new_intent, meaning: don’t hijack an old flow; let the main model start or route a new line of work (and the focal id can move forward for a new default).
If workstreams are waiting but no LLM is configured, the code raises an error instead of guessing silently.

4. Normalizing reference_id before tools run (apply_triage_focal_reference_to_tool_calls)
The model is bad at stable ids. So right before tools execute, this logic rewrites reference_id / referenceId on relevant tool calls (especially agent_quotes) so that:

Continuing a waiting (or in-flight) workstream keeps that id.
New or junk ids get new random hex ids.
Several agent_quotes in one turn (e.g. two trips) get different ids so they don’t collapse into one job.
A single call with no id can use the thread’s focal id as the default.
After that batch, the focal field on the workspace is updated to the last resolved id as a hint for the next turn.

In one sentence
workstreams.py keeps a per-thread map of multi-step “jobs,” shows that map to the model, optionally forces the right job when the user replies while several are waiting, and fixes tool arguments so each job has a consistent, server-friendly reference_id—including multiple parallel jobs in one turn.