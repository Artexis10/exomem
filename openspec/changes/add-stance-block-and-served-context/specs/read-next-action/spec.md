## ADDED Requirements

### Requirement: Read responses carry one next move

Every `recall_context` and `search_memory` response SHALL carry `next_action` inside its structured result when a state of the state table applies, and SHALL omit it otherwise. The field SHALL reuse the name of the write terminal's `next_action` and widen its shape additively to `{state, say, tool?, args?}`:

- `state` is a state of the state table;
- `say` is one move in plain words, at most 240 characters;
- `tool` names the one tool that the move calls, by the name that `capabilities` exposes to the caller's surface, and appears only when the move calls one tool;
- `args` appears only when the server can fill every argument of that call.

The server SHALL add `next_action` after the packet cache lookup and SHALL never store it in a cached packet. A maps packet SHALL always reserve 240 characters of `max_chars` for `next_action`, whatever the state, so the response stays within `max_chars` and the cached packet does not depend on the state. When the state table fails, the response SHALL omit `next_action`, its diagnostics part SHALL name `next_action_unavailable`, and the response SHALL still be served. Every tool call that `say` names SHALL be rendered through `capabilities.active_call_text`, so a move names only a call that the caller can make.

#### Scenario: An empty slice gets one move

- **WHEN** a maps packet's slice state is `empty` and no state with higher precedence applies
- **THEN** the response carries `next_action` with `state = "slice_empty"`, a `say` of at most 240 characters that names the `search_memory` call as the caller's surface writes it, and `tool` set to that surface's search tool name

#### Scenario: No state means no field

- **WHEN** a maps packet has a ready slice, no ambiguity and no stance line due for review
- **THEN** the response carries no `next_action`

### Requirement: One state table holds every per-state move

One server module SHALL map a closed set of response states to their moves, with this fixed precedence for `recall_context`, first match winning:

1. `ambiguous`: the packet abstained as `ambiguous`, or as `unresolved` with at least one candidate reached by the turn's own words; the move asks for a call again with `anchor` set to the ref the agent means;
2. `review_due`: a served stance line carries `review_due`; the move asks the agent to apply the line and to say that it is due for review;
3. `slice_empty`: the slice state is `empty`; the move says that the map and the slice cover compiled material only, and names `search_memory` as the route to captured Sources.

For `search_memory`, the state `search_miss` SHALL apply when a search returns no result, and its move SHALL carry the bootstrap `miss_rule`, which moves here.

The states SHALL be protocol tokens that this specification fixes. The module SHALL be the only copy of per-state guidance: the retrieve hook, the server instructions, the tool descriptions and the skill SHALL NOT repeat a per-state move. The server SHALL NOT branch on a client's name.

#### Scenario: Precedence picks one move

- **WHEN** a maps packet serves a stance line that carries `review_due` and its slice state is `empty`
- **THEN** the response carries exactly one `next_action`, with `state = "review_due"`

#### Scenario: A search miss carries the miss rule

- **WHEN** `search_memory` returns no result
- **THEN** the response carries `next_action` with `state = "search_miss"`, and bootstrap no longer serves `miss_rule` as a separate field

### Requirement: Static surfaces point at the next move

The server instructions, the `recall_context` and `search_memory` tool descriptions, and the skill scaffold's recall loop SHALL each carry the sentence "Follow `next_action` when present." in place of the per-state moves that the state table now holds. The server instructions SHALL stay within their 900-character bound.

#### Scenario: The tool description holds no per-state move

- **WHEN** a client lists the `recall_context` tool
- **THEN** its description carries "Follow `next_action` when present." and does not repeat the moves for `ambiguous` or `slice_empty`
