## MODIFIED Requirements

### Requirement: Compact write responses may carry one bounded capture-sweep advisory

Default compact responses to a committed durable write — the compiled page writers `remember`, `edit_memory`, `observe_memory` and `replace_memory`, and the structured writers `record_memory` and `plan_memory` — MAY carry the capture-sweep advisory as one `next_action` sentence that asks the agent to check the recent conversation once for anything else worth keeping, to save what memory lacks, and to stay silent when nothing qualifies. The default response SHALL NOT carry the producer's `capture_sweep` block: its `boundary`, `rule`, `consider` and `written_recently` keys stay at the post-commit seam and in the leaf. A call with `response_detail="full"` SHALL carry the block under `diagnostics.capture_sweep`. The block's `unpaged_mentions` names, at most five, travel as one top-level `unpaged_mentions` list beside the `next_action` sentence, because they are data the pass acts on. A response that already carries a `next_action` sentence of its own SHALL carry both in the one `next_action`, its own first and the capture-sweep sentence after it, and SHALL keep the `unpaged_mentions` list.

The advisory SHALL follow the established advisory posture: produced at the post-commit seam, validated and bounded again at the mutation terminal rather than trusted, never a key a client branches on for the mutation outcome, and absent — never null, never empty — when there is nothing to say. It SHALL NOT alter the result fields, mutation identity, or replay behaviour. The `legacy` response detail SHALL omit it. Tool descriptions, tool input schemas and the packaged tool-surface digest SHALL NOT change.

A fault anywhere in producing, validating or attaching the advisory SHALL cost the caller the advisory and never the write.

#### Scenario: The first write after a quiet interval carries the block

- **WHEN** a caller commits a durable write and that caller has not written durably within the quiet interval
- **THEN** the default compact response carries the capture-sweep `next_action` sentence, any `unpaged_mentions` the page reached for, and no `capture_sweep` key

#### Scenario: A write that already has its own next step keeps both

- **WHEN** a caller commits a durable write that carries its own `next_action` sentence and the capture-sweep ledger judges it the first of a new episode
- **THEN** the `next_action` reads the write's own sentence followed by the capture-sweep sentence
- **AND** the response still carries any `unpaged_mentions`

#### Scenario: Full detail carries the capture-sweep block

- **WHEN** the same first write is made with `response_detail="full"`
- **THEN** `diagnostics.capture_sweep` holds the producer's block

#### Scenario: A following write inside the interval is silent

- **WHEN** the same caller commits a second durable write inside the quiet interval
- **THEN** that response carries no capture-sweep `next_action` sentence and the write is otherwise unchanged

#### Scenario: The legacy detail omits the advisory

- **WHEN** a write that produced the block is projected at `legacy` detail
- **THEN** the returned leaf contains no `capture_sweep` key

#### Scenario: A malformed or oversized block is dropped rather than widened

- **WHEN** a leaf attaches a block whose boundary is outside the closed vocabulary, or whose references or mentions exceed their bounds
- **THEN** the terminal discards the whole block, never repairing it to fit, emits no `next_action` sentence for it, and the response stays valid

#### Scenario: A multi-write command emits at most once

- **WHEN** one product-command invocation commits many governed writes
- **THEN** the invocation's response carries at most one capture-sweep `next_action` sentence

### Requirement: The capture-sweep advisory is governed by a caller-scoped quiet interval

The advisory SHALL be governed by its own emission rule and SHALL NOT change the due-state governor, its batch scope, or its first-surfaced ledger. The rule SHALL be a quiet interval over an in-memory ledger keyed on the calling principal scope, the calling client, and the vault; a first-ever write from a key SHALL qualify as after-quiet. Every successful durable write SHALL update the ledger whether or not the advisory was emitted. The ledger SHALL be bounded and SHALL NOT be persisted, so a restart re-arms each caller once rather than letting a durable file decide what an agent is told. The interval SHALL be a module constant with no environment override.

Where the transport supplies no usable stable caller scope — an HTTP call whose scope is session-derived or absent — the system SHALL emit nothing rather than collapse distinct callers into one bucket. Outside any call, where identity is absent by design, a process-lifetime key SHALL apply.

The advisory SHALL be emitted only while the resolved delegation envelope permits proactive capture, read through the existing prominence and envelope resolution rather than a parallel table.

#### Scenario: Distinct callers are not collapsed

- **WHEN** an HTTP call carries no stable principal or credential scope
- **THEN** no capture-sweep `next_action` sentence is emitted for that call

#### Scenario: A quiet profile never emits

- **WHEN** the resolved envelope disposition for proactive capture is `off`
- **THEN** no response carries the capture-sweep `next_action` sentence

#### Scenario: The ledger records a write that emitted nothing

- **WHEN** a durable write is committed while the caller is inside the quiet interval
- **THEN** the ledger still records that write, so the interval is measured from the latest write rather than the latest advisory

## ADDED Requirements

### Requirement: Only bootstrap carries the due-state block

Only the bootstrap payload SHALL carry the `due_state` block: per-category open counts and a bounded list of top item references drawn from the maintained projection for the requesting audience. No search reply and no mutating response, at any `response_detail`, SHALL carry it, so a reply never tells the agent about review work unrelated to its call; the review queue serves the same items on request. The block SHALL follow the established advisory posture: bounded in size, never a key a client branches on, and absent — never null or empty — when there is nothing to report. A recall packet MAY name at most one due item that sits on material the packet returned, as `relevant_due_refs`. Tool input schemas SHALL NOT change.

#### Scenario: A write reply never carries the block

- **WHEN** a prediction became due earlier in the session and any compiled write commits
- **THEN** the compact, full and legacy responses of that write carry no `due_state` block
- **AND** the review queue and bootstrap still report the prediction

#### Scenario: A search reply never carries the block

- **WHEN** a prediction is due and `search_memory` runs
- **THEN** the reply carries no `due_state` block
- **AND** the review queue and bootstrap still report the prediction

### Requirement: An MCP refusal names what happened to the data and the exact retry

A tool call that the MCP surface refuses, a rendered tool included, SHALL return `{"error": {"code", "message", "retry"?, "candidates"?}}` and nothing else at the top level, except `diagnostics` when the call asked for `response_detail="full"`. `code` SHALL be the specific code of the refusal. When the semantic contract blocks a write, the umbrella `SEMANTIC_CONTRACT_BLOCKED` SHALL NOT be the code: the code of the finding it blocked on SHALL be promoted. `message` SHALL say what was refused. When the refusal has no `retry`, the message SHALL carry the whole of its remediation after that, because the remediation is the only fix the agent gets; a refusal with a `retry` SHALL keep its message short. A refusal that names the fields it failed on SHALL name at most three of them, then "and N more". A refused write's message SHALL end with what happened to the data: "Nothing was saved.", "The change was saved." or "Whether it was saved is not known.". The producer states that sentence when its outcome is none of these: a refused records write that kept the candidate as a held candidate SHALL say so, and SHALL NOT say "Nothing was saved.". A refusal that states its own outcome, such as `MUTATION_BUSY`, SHALL keep that one sentence.

`retry` SHALL be the exact next call, and SHALL be absent when no retry can succeed. It holds `tool`, and `arguments` only for the arguments to add or change. `retry.arguments` SHALL be a delta that the agent merges key by key, nested objects included, into the arguments it sent; every other argument is unchanged. A `null` value in the delta clears the argument it names. A value the agent must supply SHALL be a placeholder in angle brackets. `retry.after_s` SHALL give the whole seconds to wait when the refusal names a wait. The producer SHALL state the next call in command terms, and the refusal seam SHALL spell it with `capabilities.active_spell_call`, so the tool name and the placement of every argument come from the surface that served the call and never from the producer's own signature. When the next call is to a different tool, `retry.arguments` SHALL hold the whole call, because the agent has no sent arguments to merge into. `retry` SHALL be absent when the spelled delta is empty. A refusal that leaves a resumable handle SHALL put it in `retry`: a held candidate's id, or the id of the running job.

`candidates` SHALL hold at most five titles or valid registry values the agent can choose from, and SHALL be absent when there are none. They come from the producer's own `candidates`, then its `allowed` set, then the `key` of each of its ranked `options`. A refusal that names the field it failed on SHALL keep that field and its repair in the message.

The refusal's operational detail SHALL move under `diagnostics`, shown only with `response_detail="full"`: the umbrella code, the remediation prose, the duplicate `success`, `ok`, `error_code`, `status` and `committed` keys, and the coordinator, cause, waits, absorbed attempts and ids. The CLI and REST facades SHALL keep their `{success, data}` and public error blocks unchanged.

#### Scenario: A busy write names the wait and the retry

- **WHEN** a write finds the vault held by another write for thirty seconds
- **THEN** the error reads "Another write held memory for 30 seconds, so this was not saved. Retry the same call."
- **AND** its `retry` names the same tool with an `after_s` wait and carries no `arguments`

#### Scenario: An unlinked page names the decision it needs

- **WHEN** `create_note` is refused because the page links to no other page and records no reviewed-none decision
- **THEN** the code is `RELATION_DISPOSITION_MISSING`, not `SEMANTIC_CONTRACT_BLOCKED`
- **AND** `retry.arguments` holds `relation_disposition` and `relation_review_reason` with a placeholder reason, placed where `create_note` takes them

#### Scenario: An edit that outdates a saved decision names the same retry

- **WHEN** an edit is refused with `RELATION_DISPOSITION_STALE` because the page changed its connections after a reviewed-none decision
- **THEN** `retry.arguments` holds the same `relation_disposition` and `relation_review_reason` placeholders, placed where the edit tool takes them

#### Scenario: A refusal with no retry carries its whole fix

- **WHEN** an approval is refused because the page changed since it was read
- **THEN** the error reads "The page changed since it was read. Read the page again, then approve its current content_hash. Nothing was saved."
- **AND** the error carries no `retry`

#### Scenario: A stale decision on an observation retries inside its operation

- **WHEN** `change_observations` is refused with `RELATION_DISPOSITION_STALE`
- **THEN** `retry.arguments` holds `relation_disposition` and `relation_review_reason` inside `operation`, where that tool takes them
- **AND** the agent's own call, merged with `retry.arguments` and the reason filled in, commits

#### Scenario: A refused records write keeps its held candidate

- **WHEN** a records append is refused because the item carries an undeclared field
- **THEN** the message names the field and says the candidate is kept as a held candidate, and does not say "Nothing was saved."
- **AND** `retry` carries the same tool with the `held` id, so that calling it resumes the candidate with that field cleared

#### Scenario: A ranked menu reaches the agent as candidates

- **WHEN** a write is refused because it adds a generic relation, and the producer ranks specific relations as `options`
- **THEN** `candidates` holds the keys of the first five options
- **AND** the full ranked menu and its routes sit under `diagnostics` with `response_detail="full"`

#### Scenario: Full detail keeps the old envelope

- **WHEN** the same refusal is produced with `response_detail="full"`
- **THEN** `diagnostics` holds the umbrella code, the remediation prose and every operational key the previous error carried

### Requirement: A default search reply leads with a summary and carries one notice

The default `search_memory` reply SHALL be an object that leads with `user_summary`, one sentence that counts the matches, then `hits`. A hit SHALL carry only what the agent reads or passes to a later call. It SHALL carry the page `ref` beside its path, because `record_episode` takes only refs. It SHALL NOT carry a `scope` label or a unit's `parent_ref`, because the unit's own reference names the parent. The reply SHALL NOT carry `due_state`, `warming`, `degraded` or `budget` blocks. When results may really be incomplete — the store is unreadable, a page saved in the last minute may be missing, an index is still warming, the answer is degraded or the budget cut it short — the reply SHALL carry exactly one `notice` sentence that says so, and SHALL carry none otherwise. A call that asks for `include_timings` or `explain` SHALL still receive the raw `warming`, `degraded` and `budget` detail it asked for. The reply SHALL keep `next_action` and `continuation`, because the tool descriptions name them.

#### Scenario: A warm search is short and quiet

- **WHEN** a search runs against a warm index with no pending pages
- **THEN** the reply holds `user_summary` and `hits`, each hit carries its path and no `ref` or `scope`
- **AND** the reply carries no `notice`, `warming` or `due_state`

#### Scenario: A search that may be incomplete says so once

- **WHEN** a search runs while pages saved in the last minute are not yet indexed
- **THEN** the reply carries one `notice` sentence saying recently saved pages may be missing
- **AND** it carries no `warming` block unless the call asked for timings or an explanation

## REMOVED Requirements

### Requirement: Compact responses may carry one bounded advisory due-state block

**Reason**: Write and search replies carry no due-state block. The block described review work unrelated to the call, which the agent could neither act on nor pass to its next call. "Only bootstrap carries the due-state block" replaces it.

**Migration**: Read the counts from `bootstrap` or `review_memory`. A recall packet names the one due item that fits its turn as `relevant_due_refs`.

### Requirement: Operation leaves are due-state carriers

**Reason**: A write reply carries no due-state block. The block described review work unrelated to the call, which the agent could neither act on nor pass to its next call. Its homes stay bootstrap, recall and the review queue.

**Migration**: Read the counts from `bootstrap`, `recall_context` or `review_memory`. A program that needs the bulk write's own outcome reads the operation's result keys, which this change leaves unchanged.

### Requirement: Structured-collection mutations are due-state carriers

**Reason**: Same decision as for operation leaves: `record_memory` and `plan_memory` write replies carry no due-state block.

**Migration**: Read the counts from `bootstrap`, `recall_context` or `review_memory`.
