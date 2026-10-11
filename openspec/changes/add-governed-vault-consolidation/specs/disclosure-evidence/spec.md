## ADDED Requirements

### Requirement: Connector and import evidence remains plaintext-free

Connector admission and later import evidence SHALL use existing receipt owners and plaintext-free schemas. Receipts SHALL NOT contain bodies, private paths, titles, references, raw client or principal identifiers, policy text, credentials, or token bytes. Private detailed findings SHALL stay in protected operational state outside ordinary recall.

#### Scenario: A negative disclosure check fails

- **WHEN** verification detects forbidden content or a metadata leak
- **THEN** public evidence records only the bounded outcome permitted by its schema
- **AND** expected and observed private bytes remain outside receipts, logs, and ordinary recall

### Requirement: Receipt access obeys the connector ceiling

An owner connector SHALL NOT observe hidden corpus facts through receipts, provenance, run inventory, or aggregate evidence. Returned evidence SHALL use admitted contributors or an existing unavailable result. Global hashes, counts, or event changes influenced by hidden content SHALL NOT become a disclosure channel.

#### Scenario: A hidden write appends a receipt

- **WHEN** a protected write changes an evidence chain
- **THEN** a restricted connector cannot infer that write from a returned global head, count, or receipt detail
- **AND** owner status alone does not authorize the hidden observation

### Requirement: Operational evidence is neither policy nor knowledge

Armed requirements, import state, private staging, journals, and receipts SHALL remain outside knowledge indexing and policy input. Fixed content-census exclusions SHALL separate operational churn from canonical content. Receipt or run updates SHALL NOT change a content snapshot merely by being appended.

#### Scenario: Receipts change while the corpus stays fixed

- **WHEN** evidence appends without a canonical content or policy change
- **THEN** the content fingerprint remains unchanged
- **AND** policy compilation and ordinary recall do not ingest the new evidence as knowledge

### Requirement: Recovery preserves append-only evidence

Import, abort, rollback, restore, and software downgrade SHALL preserve append-only receipt history. Recovery SHALL reconcile exact effect fingerprints through existing durable journal and receipt contracts. It SHALL NOT infer one store's commit from another store's claim or restore an earlier evidence head.

#### Scenario: Restoration uses an earlier preimage

- **WHEN** rollback restores canonical data from a preimage made before import
- **THEN** later import and rollback evidence remains present and chain-valid
- **AND** restoration never truncates receipt history or deletes the armed compatibility guard

#### Scenario: A journal and receipt disagree

- **WHEN** recovery finds a missing or divergent durable terminal
- **THEN** it reconciles against the exact recorded effect state before advancing
- **AND** it does not repeat semantic decisions or report an unverified effect as complete

### Requirement: Duplicate and retirement evidence states what survives

Import evidence SHALL retain a durable source-to-destination mapping for exact duplicates, including publication no-ops. Retirement evidence SHALL distinguish proposed, authorized, and observed effects and state which bytes and provenance survive. It SHALL NOT claim external deletion or routing changes merely because product verification passed.

#### Scenario: Exact duplicate needs no publication

- **WHEN** reconciliation reuses an existing byte-identical destination object
- **THEN** protected provenance retains the source snapshot and exact destination mapping
- **AND** plaintext-free evidence can bind that mapping without exposing its private fields

#### Scenario: Retirement has only been authorized

- **WHEN** the operator authorized source retirement but no external completion was observed
- **THEN** evidence reports authorization without claiming destruction, routing shutdown, or account changes
