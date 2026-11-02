## ADDED Requirements

### Requirement: Read responses carry one next move

Every `recall_context` and `search_memory` response SHALL carry `next_action` inside its structured result when a state of the state table applies, and SHALL omit it otherwise. The field SHALL reuse the name of the write terminal's `next_action` and widen its shape additively to `{state, say, tool?, args?}`:

- `state` is a state of the state table;
- `say` is one move in plain words, at most 240 characters;
- `tool` names the one tool that the move calls, by the name that `capabilities` exposes to the caller's surface, and appears only when the move calls one tool;
- `args` appears only when the server can fill every argument of that call.

The server SHALL add `next_action` after the packet cache lookup and SHALL never store it in a cached packet. A maps packet SHALL always reserve 240 characters of `max_chars` for `next_action`, whatever the state, so the response stays within `max_chars` and the cached packet does not depend on the state. When the state table fails, the response SHALL omit `next_action` and SHALL still be served. Every tool call that `say` names SHALL be rendered through `capabilities.active_call_text`, so a move names only a call that the caller can make.

#### Scenario: An empty slice gets one move

- **WHEN** a maps packet's slice state is `empty` and no state with higher precedence applies
- **THEN** the response carries `next_action` with `state = "slice_empty"`, a `say` of at most 240 characters that names the `search_memory` call as the caller's surface writes it, and `tool` set to that surface's search tool name

#### Scenario: No state means no field

- **WHEN** a maps packet has a ready slice, guidance state `none_reviewed` and no line due for review
- **THEN** the response carries no `next_action`

### Requirement: One state table holds every per-state move

One server module SHALL map a closed set of response states to their moves, with this fixed precedence for `recall_context`, first match winning:

1. `correction`: the request is a task refresh with `refresh_reason = "correction"`; the move offers to save the correction and to propose the exact stance line or precedent with its source;
2. `ambiguous`: the packet abstained as `ambiguous`, or as `unresolved` with at least one candidate reached by the turn's own words; the move asks for a call again with `anchor` set to the ref the agent means;
3. `guidance_selected`: the guidance state is `selected`; the move asks the agent to read the named guidance before a substantive conclusion, to check the draft against each served guidance item, and to state in one line which items the answer applied or set aside, by ref;
4. `guidance_unknown_activity`: the guidance state is `unknown_activity`; the move asks for a refresh that declares a key from the activity index;
5. `guidance_none_declared`: the guidance state is `none_declared` and the activity index is not empty; the move says that maps never pre-select task context and asks for a task refresh that declares the activity from the index once the task kind is known;
6. `review_due`: a served stance line or guidance item carries `review_due`; the move asks the agent to apply it and to say that it is due for review;
7. `slice_empty`: the slice state is `empty`; the move names `search_memory` as the route to captured Sources.

For `search_memory`, the state `search_miss` SHALL apply when a search returns no result, and its move SHALL carry the bootstrap `miss_rule`, which moves here.

The states SHALL be protocol tokens that this specification fixes. The module SHALL be the only copy of per-state guidance: the retrieve hook, the server instructions, the tool descriptions and the skill SHALL NOT repeat a per-state move. The server SHALL NOT branch on a client's name.

#### Scenario: Precedence picks one move

- **WHEN** a task refresh carries `refresh_reason = "correction"` and its packet also has guidance state `selected` and an empty slice
- **THEN** the response carries exactly one `next_action`, with `state = "correction"`

#### Scenario: Guidance selected asks for the check

- **WHEN** the guidance state is `selected` and no correction or ambiguity applies
- **THEN** `next_action.state` is `guidance_selected` and its `say` asks the agent to check the draft against each served guidance item and to name, by ref, the items it applied or set aside

#### Scenario: A search miss carries the miss rule

- **WHEN** `search_memory` returns no result
- **THEN** the response carries `next_action` with `state = "search_miss"`, and bootstrap no longer serves `miss_rule` as a separate field

### Requirement: Task refresh carries a closed reason

A `recall_context` call with `trigger = "task_refresh"` SHALL accept an optional `refresh_reason` from the closed set `new_evidence`, `task_change` and `correction`, which this specification fixes, on MCP, the CLI and the REST facade over one leaf. The value SHALL change no selection, ranking or authority; it SHALL only select the `correction` move. An unknown value, or a value sent without `trigger = "task_refresh"`, SHALL be ignored, and the response's diagnostics part SHALL name `refresh_reason_ignored` with the accepted values.

#### Scenario: A correction refresh asks to save the correction

- **WHEN** an agent calls `recall_context` with `trigger = "task_refresh"` and `refresh_reason = "correction"`
- **THEN** the packet equals the packet for the same refresh without a reason, and `next_action.state` is `correction`

#### Scenario: A misplaced reason is ignored

- **WHEN** a `user_turn` call carries `refresh_reason = "correction"`
- **THEN** the call is served as without it, and the diagnostics part names `refresh_reason_ignored`

### Requirement: Static surfaces point at the next move

The server instructions, the `recall_context` and `search_memory` tool descriptions, and the skill scaffold's recall loop SHALL each carry the sentence "Follow `next_action` when present." in place of the per-state moves that the state table now holds. The server instructions SHALL stay within their 900-character bound.

#### Scenario: The tool description holds no per-state move

- **WHEN** a client lists the `recall_context` tool
- **THEN** its description carries "Follow `next_action` when present." and does not repeat the moves for `ambiguous`, `guidance_selected` or `slice_empty`
