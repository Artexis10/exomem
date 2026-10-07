## ADDED Requirements

### Requirement: Compact bootstrap serves a core and on-demand sections

For every surface whose bootstrap contract is not published as a frozen hosted profile, `bootstrap` with `profile="compact"` SHALL return a core operating contract and an index of named sections, not the full reference payload. The core SHALL carry every rule named in the core rule manifest at every engagement level the manifest assigns it to. Each section SHALL be retrievable through `bootstrap(profile="compact", section=<name>)` and SHALL contain the blocks it names byte-identically to their pre-core form. `section="index"` SHALL list every section with its size in served-JSON bytes. The core SHALL name every section with its size, and its workflow loop SHALL name the section that holds `vocabulary_workflow`. The vault-derived blocks SHALL be bounded in the core: `due_state` as a counts summary (total and largest category, at most 200 bytes) that names the `epistemics` section holding the list, `latency` not at all (it is served by `diagnostics_reading`, and to a session client), and the vocabulary as a pointer to the `vocabulary` section, never as a list of entity types (amended by `add-vocabulary-registries`, which renamed the `entities` section). The core's digest of the delegation envelope SHALL keep the unclassified-action rule and name the section that holds the full protocol. An unknown section SHALL fail with a validation error naming the accepted sections. The `full` and `diagnostics` profiles SHALL be unchanged. The `session` profile SHALL serve the core's forms of `engagement` (the delegation envelope digested, its full protocol in the `envelope` section) and `server` (`compute_policy` moved to the `diagnostics_reading` section), plus the `sections` index, and is otherwise unchanged; its served size moves from 21,974 to 21,270 bytes.

#### Scenario: The core carries the incident-preventing rules at every level that owes them

- **WHEN** compact bootstrap is served at any engagement level on any non-frozen surface
- **THEN** it carries the recall-before-answering rule at `balanced` and `maximal`, the capture loop, the episode-recording pass at `balanced` and `maximal`, the epistemic commitments, the delegation ceiling, and the due-state restraint

#### Scenario: A section returns what compact used to return

- **WHEN** a client calls `bootstrap(profile="compact", section="authoring")`
- **THEN** the response carries the authoring blocks byte-identically to the pre-core compact payload
- **AND** the union of the core and every section contains every pre-core leaf except those the manifest lists as replaced by a digest, each of which names the section holding the original

#### Scenario: An unknown section is refused

- **WHEN** a client calls `bootstrap(section="nonexistent")`
- **THEN** the operation fails with a validation error naming the accepted sections

### Requirement: The recall rule names only commands the surface exports

Where the active surface does not export `activate_context`, the compact bootstrap SHALL carry the recall contract with the recall carrier line naming `ask_memory` in its place, rather than losing the contract to the surface filter. Every unpublished surface's compact bootstrap SHALL carry a recall rule at every engagement level. Released hosted profiles SHALL keep their published payload.

#### Scenario: A hosted surface still receives the recall rule

- **WHEN** compact bootstrap is served at `balanced` or `maximal` on a surface that exports `ask_memory` but not `activate_context`
- **THEN** `engagement.contract.recall` is present, tells the agent to search memory, names `ask_memory`, and does not name `activate_context`

### Requirement: Compact core stays under a derived byte budget

The compact core SHALL stay at or under its ruled byte ceiling at every engagement level on every non-frozen surface, including the hook-capable worst case, measured on a populated vault at `maximal` (32 custom entity types, `due_state` and `latency` at their worst) as well as an empty one; the populated core SHALL stay at or under the ceiling, and the empty-vault core SHALL warn inside the 512-byte band below it. The ceiling SHALL be derived from the core allocation with a documented margin, not from the size the payload happens to have. Every section SHALL also stay at or under its own ceiling. Raising any ceiling SHALL require an entry in the core rule manifest or an argument recorded beside the constant that the added bytes are needed on every session.

#### Scenario: A new block cannot grow the core silently

- **WHEN** a change adds bytes to the core past its headroom warning
- **THEN** the budget test warns before the ceiling and fails at it
- **AND** the change must name the manifest rule the bytes carry

### Requirement: Released hosted profiles keep their bootstrap payload

`hosted-alpha-agent-v1` through `hosted-alpha-agent-v4` SHALL serve the same compact bootstrap payload after the core split as before it, at every engagement level, and SHALL keep their pinned `bootstrap` parameter schema without a `section` argument. The released hosted plugin candidates and their compatibility descriptors for those profiles SHALL NOT change.

#### Scenario: A frozen profile is byte-stable across the split

- **WHEN** compact bootstrap is served under a frozen hosted profile at any level
- **THEN** its digest, with only the server version and tool-surface digests normalised, equals the digest recorded before the split

### Requirement: Injected agent context stays inside a per-item byte budget

Every text Exomem injects into a coding agent's context without the agent asking for it, namely the Stop-hook capture and episode checks, the `UserPromptSubmit` retrieval reminder and the continuation checkpoint, SHALL stay at or under a ruled byte ceiling per item, asserted by test. The retrieval reminder SHALL send its full text once per session and again after a session lifecycle event (a compaction rewrites the context the text lived in), and a short line on later fires (the Stop capture check is short on every fire, per `shorten-stop-hook-blocks`); at `balanced` the retrieval reminder SHALL be silent between, and at `maximal` it SHALL be a short pointer on every later prompt. A short nudge SHALL keep the rule it exists to carry (the capture trigger with the live-policy pointer, the `episode_memory` record call with its key and its do-nothing escape, and the `ask_memory` recall with its skip escape and its not-found-in-scope reading). The retrieval reminder SHALL also keep the rules not to repeat a search because the reminder recurs and that the KB is the source of truth for prior conclusions, and the maximal pointer SHALL keep the already-covered escape. The capture and retrieval hooks SHALL resolve their state home exactly as the continuation checkpoint does (`EXOMEM_HOOK_HOME`, then `CLAUDE_CONFIG_DIR` or `CODEX_HOME`), so a compaction re-arms them wherever the client keeps its configuration. The hook scripts under `src/exomem/_hooks/` and their plugin mirrors SHALL remain byte-identical.

#### Scenario: A nudge cannot regrow into the old text

- **WHEN** a hook constant exceeds its byte ceiling or drops a rule named in its test
- **THEN** the budget test fails naming the constant

#### Scenario: The full text is sent once, then a short line

- **WHEN** the retrieval reminder is due on three prompts in one session at `balanced`
- **THEN** the first carries the full text and the next two are silent
- **AND** after a compaction the next carries the full text again

## MODIFIED Requirements

### Requirement: Bootstrap surfaces a recall latency regression to the client experiencing it

The `bootstrap` response SHALL surface a recall latency regression only while the latency watch reports a breach for the calling client. The full `latency` block SHALL list, per breaching tool, the tool name, the deep flag, the sample count, `p50_ms`, `p90_ms`, `ceiling_ms` and the dominant spans as name, milliseconds and call count, and SHALL be served by the `diagnostics_reading` section and by the `session` profile. The compact core SHALL NOT carry the block: while a breach is active it SHALL carry only a `latency` pointer line naming that section (about 40 bytes), and while none is active it SHALL carry no `latency` key at all. The profiles that serve no core (`full`, `diagnostics` and the released hosted profiles) SHALL carry the block itself. The block SHALL be computed from the in-process watch and MUST NOT read the ledger file or any vault content. A bootstrap response for a client with no breach SHALL be identical in shape to a response without this capability.

#### Scenario: A healthy service leaves bootstrap unchanged

- **WHEN** the watch reports no breach for the calling client
- **THEN** the bootstrap response has no `latency` key, on every profile and section

#### Scenario: A breaching client is pointed at what is slow

- **WHEN** the calling client's plain recall p90 is over its ceiling with enough samples
- **THEN** the compact core carries a `latency` pointer line naming `section=diagnostics_reading`
- **AND** `bootstrap(section="diagnostics_reading")` and the `session` profile carry the block with the tool, `samples`, `p50_ms`, `p90_ms`, `ceiling_ms` and the dominant spans
- **AND** no query text, path or excerpt appears in either

#### Scenario: Another client's breach is not this client's

- **WHEN** only a different client's recalls are over the ceiling
- **THEN** this client's bootstrap response has no `latency` key
