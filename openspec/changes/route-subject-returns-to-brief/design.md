# Design

## Context

The topic brief shipped in `add-topic-brief`. That change's development evaluation (2026-11-22) found two things:

- A routing line in the body of the client skill never reached the model. No run opened the skill, and the model followed the server's instructions instead.
- With the line given to the model directly, the model called the brief, but every call answered `index_warming`. The service's graph read snapshot waited on its background cold proof, which a CPU limit on the service delayed. The run therefore measured failed brief calls, not the routing line.

Source consulted: the "Procedural knowledge is overloading global instructions" row of the agentic-engineering routing table (Agent Skills; Effective context engineering). A skill's body loads only when the skill is invoked; its description and the server's instructions are always in context.

## Decisions

### D1. The routing clause goes into the server's instructions

Three places are always in context: the server's instructions, the `read_page` tool description, and the client skill's description. The server's instructions reach every MCP client, chat apps included, and already carry the recall loop ("search_memory/read_page: gaps"). The clause joins that loop there. The skill's description reaches only clients that load the plugin, and the tool description is read when the model weighs a call, after it has already chosen recall.

The clause names the request in plain words and leaves it to the model to recognise one; the server does not match words in the turn.

### D2. The clause stays short

Some clients cut server instructions off, so the clause adds the fewest words that name the request, the call and the gap rule. The full contract stays in the skill references.

### D3. Evaluate before release, against a serving brief

The evaluation reuses the closed change's protocol, keys and dev split:

- Arm A: today's server instructions. Arm B: the instructions plus the clause, given through the system prompt, which is where a client places server instructions.
- Before each cell, a read of one dev subject's brief must answer `ready`. A brief call that answers `index_warming` during a cell makes the cell an infrastructure failure: one retry with identical inputs, then the subject drops from both arms.
- Run it while the personal service runs without its CPU limit.
- A development round (5 subjects, one draw) first. Then the decision run on held-out subjects, under a budget the operator agrees first.
- Every paid run is recorded in the personal eval-run collection.

## Risks

- The clause could pull the brief into requests that are not returns to a subject, which costs a call and a longer answer. The evaluation's free measures (brief calls per run, tokens, wall time) and the contradiction rate watch for this.
- A same-family judge (Claude judging a Claude actor) is recorded as a limitation where no other family is available.
