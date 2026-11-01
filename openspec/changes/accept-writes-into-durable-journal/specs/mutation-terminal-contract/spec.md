## ADDED Requirements

### Requirement: Accepted Is A Nonterminal State
The closed mutation state set SHALL include `accepted`: a write that the system recorded in the vault's intent journal and has not committed yet. An accepted response SHALL carry `ok=true`, `state="accepted"`, `terminal=false`, `status="accepted"`, `committed=false` and `mutated=false`. It SHALL also carry `intent_id`, the intent reference, `request_id`, the receipt identity when one exists, `validation`, the planned paths, `warnings_count` and a next action. It SHALL NOT carry `status="committed"` or `mutated=true`. The terminal that the intent later reaches SHALL carry the same `intent_id`. `operation_id` SHALL keep its meaning as the correlation of a validated draft with its commit.

#### Scenario: An accepted write later commits
- **WHEN** a write is answered `accepted` and the applier then commits it
- **THEN** a poll returns a committed terminal with the same `intent_id`
- **AND** the committed terminal has the same compact fields as an uncontended commit

#### Scenario: REST answers an accepted write
- **WHEN** a REST write is answered with the accepted envelope
- **THEN** the HTTP status is 202 and the envelope reports success

## MODIFIED Requirements

### Requirement: Pre-Commit And Uncertain Errors Remain Unambiguous
The terminal projector MUST NOT convert rejected, busy, pending, accepted, committed-failure, or committed-uncertain outcomes into successful committed result dictionaries. `MUTATION_BUSY` SHALL remain retryable only when no commit occurred, and post-commit uncertainty SHALL retain its committed semantics and same-identity remediation. For a write that is eligible for the intent journal, `MUTATION_BUSY` SHALL be returned only when the journal cannot accept the write, with `cause` `journal_full` or `journal_unavailable`. A held boundary SHALL yield the committed or accepted state instead. Committed-uncertain SHALL be returned only when the canonical commit is proven and persisting the terminal record itself failed; a failure in derived-state acknowledgement after a persisted terminal is not uncertainty and SHALL NOT produce this error.

#### Scenario: Mutation boundary is busy
- **WHEN** boundary acquisition times out before the command leaf runs, for a write that is not eligible for the intent journal
- **THEN** the call fails with structured `MUTATION_BUSY`, `status="retryable"`, and `committed=false`
- **AND** no success terminal is returned

#### Scenario: An eligible write meets a busy boundary
- **WHEN** boundary acquisition times out for a write that is eligible for the intent journal
- **THEN** the call returns the committed terminal or the accepted state
- **AND** it does not return `MUTATION_BUSY`

#### Scenario: The intent journal cannot accept
- **WHEN** an eligible write cannot commit inline and the intent journal is full or unavailable
- **THEN** the call fails with `MUTATION_BUSY`, `status="retryable"`, `committed=false` and a `cause` of `journal_full` or `journal_unavailable`
- **AND** no success terminal is returned

#### Scenario: Exact post-commit terminal is unavailable
- **WHEN** canonical commit occurred but exact terminal persistence cannot be proven
- **THEN** the existing committed-uncertain error is returned
- **AND** the caller is told not to retry with a new identity

#### Scenario: Terminal persistence fails but acknowledgement would have succeeded
- **WHEN** canonical commit occurred and deterministic fault injection makes terminal persistence fail
- **THEN** the call returns committed-uncertain with `status="committed"` and `committed=true`
- **AND** a same-identity retry resolves the canonical row rather than executing the leaf again
