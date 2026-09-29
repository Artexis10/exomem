## ADDED Requirements

### Requirement: Compact bootstrap serves a core and on-demand sections

For every surface whose bootstrap contract is not published as a frozen hosted profile, `bootstrap` with `profile="compact"` SHALL return a core operating contract and an index of named sections, not the full reference payload. The core SHALL carry every rule named in the core rule manifest at every engagement level the manifest assigns it to. Each section SHALL be retrievable through `bootstrap(profile="compact", section=<name>)` and SHALL contain the blocks it names byte-identically to their pre-core form. `section="index"` SHALL list every section with its size in served-JSON bytes. An unknown section SHALL fail with a validation error naming the accepted sections. The `full`, `diagnostics` and `session` profiles SHALL be unchanged.

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

The compact core SHALL stay at or under its ruled byte ceiling at every engagement level on every non-frozen surface, including the hook-capable worst case, with the vault-derived blocks at their maximum bound. The ceiling SHALL be derived from the core allocation with a documented margin, not from the size the payload happens to have. Every section SHALL also stay at or under its own ceiling. Raising any ceiling SHALL require an entry in the core rule manifest or an argument recorded beside the constant that the added bytes are needed on every session.

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

Every text Exomem injects into a coding agent's context without the agent asking for it, namely the Stop-hook capture and episode checks, the `UserPromptSubmit` retrieval reminder and the continuation checkpoint, SHALL stay at or under a ruled byte ceiling per item, asserted by test. The capture check and the retrieval reminder SHALL send their full text once per session and again after a session lifecycle event (a compaction rewrites the context the text lived in), and a short line on later fires; at `balanced` the retrieval reminder SHALL be silent between, and at `maximal` it SHALL be a short pointer on every later prompt. A short nudge SHALL keep the rule it exists to carry (the capture trigger with the live-policy pointer, the `episode_memory` record call with its key and its do-nothing escape, and the `ask_memory` recall with its skip escape and its not-found-in-scope reading). The hook scripts under `src/exomem/_hooks/` and their plugin mirrors SHALL remain byte-identical.

#### Scenario: A nudge cannot regrow into the old text

- **WHEN** a hook constant exceeds its byte ceiling or drops a rule named in its test
- **THEN** the budget test fails naming the constant

#### Scenario: The full text is sent once, then a short line

- **WHEN** the Stop capture check fires three times in one session
- **THEN** the first fire carries the full text and the next two carry the short check
- **AND** after a compaction the next fire carries the full text again
