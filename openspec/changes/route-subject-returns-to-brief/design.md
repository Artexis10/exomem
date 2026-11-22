# Design

## Context

The topic brief shipped in `add-topic-brief`. That change's development evaluation (2026-11-22) found two things:

- A routing line in the body of an evaluation copy of the client skill never reached the model. No run opened the skill, and the model followed the server's instructions instead.
- With the line given to the model directly, the model called the brief, but every call answered `index_warming` with no items. In a managed service the brief hands the graph's cold proof to the background and abstains until it finishes (`topic_brief.py`). The run therefore measured failed brief calls, not the routing line. A CPU limit on the service is the likely reason the cold proof did not finish during the run; that is an operational diagnosis, not something the run files show.

Source consulted: the "Procedural knowledge is overloading global instructions" row of the agentic-engineering routing table (Agent Skills; Effective context engineering). A skill's body loads only when the skill is invoked; its description and the server's instructions are always in context.

## Decisions

### D1. The routing clause goes into the server's instructions

Three places are always in context: the server's instructions, the `read_page` tool description, and the client skill's description. The server's instructions reach every MCP client, chat apps included, and already carry the recall loop ("search_memory/read_page: gaps"). The clause joins that loop there. The skill's description reaches only clients that load the plugin, and the tool description is read when the model weighs a call, after it has already chosen recall.

The clause names the request in plain words and leaves it to the model to recognise one; the server does not match words in the turn.

### D2. The clause fits the 900-character bound

The `conversation-aware-activation` spec bounds the server's instructions at 900 characters and says the bound shall not be raised; `tests/test_working_set_conversation_surface.py` enforces it. Today's text uses 897 characters, so the clause cannot be appended. This change keeps the bound.

Task 1.1 writes one whole candidate text of at most 900 characters. The clause joins the existing recall-loop sentence, and other wording is shortened so that every existing instruction stays. The candidate lists each instruction whose wording it shortens. The full contract of the brief stays in the skill references.

### D3. Evaluate the exact candidate before release, against a serving brief

The evaluation reuses the closed change's protocol, keys and dev split:

- Arm A: today's server instructions. Arm B: the candidate text from task 1.1, identified by its SHA-256.
- Arm B's client receives the candidate in place of today's text, never beside it. Appending the candidate to the system prompt while the server still sends today's text would show the model both. Task 2.1 settles the mechanism, for example a thin MCP proxy that forwards every call to the personal service and answers `initialize` with the candidate. Each arm's recorded system prompt is checked to hold only its own text.
- Before each cell, a read of the brief of one subject outside the measured cases must answer `ready`. Using an outside subject keeps the readiness read from moving a measured subject's `changed_since`.
- A brief call that answers `index_warming` during a cell makes the cell an infrastructure failure: one retry with identical inputs, then the subject drops from both arms. The evaluation therefore does not measure what a client does while the brief warms.
- For each existing instruction that the candidate shortens, the development round adds one case that exercises it, so a loss there shows against arm A.
- Run it while the personal service runs without its CPU limit.
- A development round (5 subjects, one draw) first. Then the decision run on held-out subjects, under a budget the operator agrees first.
- The candidate ships only if the decision run meets all four of the closed protocol's decision rules, each on the per-subject difference (B minus A):
  1. the composite score's lower bound is above zero;
  2. conclusion coverage's lower bound is above minus L;
  3. the contradiction rate's upper bound is below plus L;
  4. arm B's mean cost per subject is at most twice arm A's, and it has at most one more arm-caused failure than arm A.

  L is in share units, proposed 0.05, and the operator confirms it before the decision run.
- Every paid run is recorded in the personal eval-run collection.

## Risks

- The clause could pull the brief into requests that are not returns to a subject, which costs a call and a longer answer. The evaluation's free measures (brief calls per run, tokens, wall time) and the contradiction rate watch for this.
- Shortened wording could weaken an existing instruction. The per-instruction cases in D3 watch for this.
- A same-family judge (Claude judging a Claude actor) is recorded as a limitation where no other family is available.
