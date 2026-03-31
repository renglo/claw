claw_implementation


This document describes how claw_spec is implemented in Renglo 


1. Context Engine (Implementation details)

- The context engine is a handler
- The context engine has a run function that receives its input in a payload. 
- The context engine will fetch data for its context as follows
    a. Static workspace context should be retrieved from beliefs.py 
        + beliefs.py calls data_controller.get_a_b > ring:claw_beliefs
    b. Session events will be retrieved from sessions.py
        + sessions.py calls sessions_controller.list_turns 
        + To avoid getting all the history or turns, it will be filtered (similar to agent_utilities.get_message_history)
    c. Memory artifacts will be retrieved from journal.py
        + journal.py calls data_controller.get_a_b > ring:claw_journal
    d. Relevant Tool schemas will be retrieved from context.py
        + context.py calls data_controller.get_a_b > ring:schd_tools to retrieve documents of a subset of tools listed in the context_engine.
    e. Runtime metadata will be included in its initialization (config, portfolio, org, etc)



2. React Loop (Implementation details)

- At the beginning of every turn (when the react is initialized), the ReactLoop will create a new turn using sessions.py
    + sessions.py calls session_controller.create_turn
- At every loop, the turn will be updated with new session events using session.py
    + sessions.py calls session_controller.update_turn
- The React Loop will call context.py to generate the prompt to be sent to the LLM
- The LLM will be called from a dedicated centralized function. 
- If a tool needs to be called the loop will call scheduler_controller.handler_call
- If a subagent needs to be called the loop will call subagents.py
    + subagents.py will take care of creating a branch session (creating a new thread on the session)
    + subagent.py abstracts out all the process required to spin an asynchronous session thread.
    + subagent.py also abstracts out the process of bringing back the result back to the main session. 
- The loop will persist events using an auxiliary function called save_event()
    + Save event calls session_controller.update_turn (similar to agent_utilities.save_chat)
    + Events are not constrained to messages but also tool calls, tool results, etc. 
- The loop will implement a utility function similar to agent_utilities.print_chat that will allow it to send streaming messages back to a WebSocket service
- loop.print_chat doesn't need to send everything to the websocket but relevant and summarize status updated (progress, loop progression, tool calling, errors, etc). 
- The loop should have a safety feature to avoid runaway loops. 
- The loop should allow opportunistic tool executions meant to update belief system and other subsystems. 



3. Memory > Belief System (Implementation details)

- The belief system is modeled with a Blueprint > ring:claw_beliefs and stored, retrieved, updated and deleted using the data_controller. 
- Each fact in the belief system lives in its own document. 
- Each document might have metadata and tags to signal belief relevance, category, creation date, etc. 
- When important facts are promoted to the belief system, the data_controller is called to generate a new belief. 
- Beliefs are derived from the activity in sessions however they are independent from them. Although it would be nice to keep a note of the provenance of a belief in the belief document (if that is available).
- Renglo caches automatically get_a_b results to S3. 


4. Memory > Journal (Implementation details)

- The Journal is modeled with a Blueprint > ring:claw_journal and stored, retrieved, updated and deleted using the data_controller. 
- All journal entries live in their corresponding task+date based document. A new daily journal document is created if it doesn't exist. 
- If the document already exists, the journal entries are appended to it. (In a list field called entries)
- A journal entry is not the same as a session event. 
- The index of the claw_journal is based on the entity_id:type_id:task:timestamp for time based retrieval
- Assuming there are two types of tasks reported in the journal: user_creation, payment. And there are both user_creation and payment related journal entries, there will be two different documents one indexed under <entity_type>:<entity_id>:user_creation:<timestamp> and another under <entity_type>:<entity_id>:payment:<timestamp> . That way the agent can go back in time and only retrieve the payment memories. 


5. SubAgent Runtime (Implementation details)

- The main agent uses an entity_type and entity_id and a thread_id as its unique key. 
- When a subagent is called, you just need to create a new thread id under the same entity_type and entity_id
- This will work as a chained list where the subagent needs to know what thread called it to be able to send messages back to it. 
- It is possible to make thread_id deterministic (not just random). We could make the new thread_id a derivation of the original one (or it could just be random)
- The subagent is running asychronously from the main thread. The mechanism that allows the subagent to emit signals has not been implemented. Propose something. 
- Bear in mind that we are running the main thread on a Python+Lambda setup. In order to truly detach the subagent we might need to create an event that spins another api call (as a new message going to the new subagent). 
- The subagent uses a websocket like interface, we could use that or a brand new one. 
- The subagent communicates back using the standard renglo chat communication {_type:"json", _out:<actual message object>, _interface:"flights"}. The agent needs to be able to interpret that (or we can rewrite the subagent to communicate with a new format)
- _interface is a good way to recognize what payload needs to be let pass "as is". 


6. Sessions

- Sessions are stored in multiple documents. Everything is handled by the sessions_controller.py
- The unique key of a session is made from entity_type, entity_id and thread. 
- From a session perspective, the session of a main agent and the session of a subagent look the same.
- A session can be reset, for that a new thread is created. That way old sessions stay in the database.
- Sometimes sessions need to be reset but that doesn't mean that memories are deleted. 
- The compaction process converts session data into memories. 
- The agent stores the messages between the user-agent or agent-agent in the session. 
- In order to reconstruct a conversation, the sessions class offers a way to retrieve the history of session events and filter out everything that is not a message. That filtering takes place with the index, not by downloading all the events and then filtering them. 


7. Compaction

- Compaction will be implemented as a handler that runs whenever it is called (manually or by schedule or by event)
- The memory flush will write to memory (belief and journal)  using the belief and journal classes (they use the data_controller class)
- There should be a record of a compaction taking place. Store it in ring:claw_compaction

























