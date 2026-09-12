# Route observed state through collection claims

## Why

Records collections are passive today: a manifest says what a collection stores, but nothing deterministic can tell that an Evidence artifact or a compiled observation *belongs* to one, so routing depends on the agent remembering the contract on every turn, coverage cannot be counted, and a recurring longitudinal domain (subscriptions changing over months across several notes) is never proposed as a collection because no sensor watches for it. Two live dogfood cases on 2026-09-09 (KB: `Notes/Research/Exomem/repeated-mutable-domains-automatic-records-promotion-routing-subscriptions-public-posts-dogfood`) showed both halves: a published post preserved as Evidence with no Record and nothing representing the gap, and months of subscription state living in prose with no ledger and no proposal.

## What Changes

- **Collection claims.** A manifest MAY declare `claims` (tags, terms, entity types, evidence kinds). Effective claims are the declared set plus a derived set from the collection's own observed enum and low-cardinality string values and item tags, maintained as a projection so old collections participate without editing. A collection with fewer than two effective terms is not a routing target.
- **Routing advisory.** A compiled-note or Evidence write whose terms cover exactly one collection's effective claims returns a bounded `records_routing` advisory naming that collection, the matched terms and the collection's natural key. Advisory, fail-open, never weakens the write, discloses only a manifest the caller may read.
- **Coverage family `unreflected_observations`.** The same match records a structured due-state entry per (collection, observation page). A record write in that collection that links the page or carries a matching natural-key value settles it; a grace window keeps an in-progress episode quiet; reconcile heals; resolution is by state change. Served through the existing carriers; `record_memory(inspect)` coverage gains `unreflected`, so a ledger reads complete, partial or blocked.
- **Promotion sensor `collection_candidate`.** An audit category over parsed compiled pages that surfaces a domain term recurring across pages and dates with state-change or amount/date-shaped units and recurring identities, when no collection's effective claims cover it. Projected into the due-state block like `question_aging`; opt-in to the attention union; resolves when a manifest claims the terms; no dismissal memory beyond the existing fingerprint rule.
- **Authority and contract.** Appends into a claiming collection stay `proactive_capture`; the advisory and the candidate are `structural_suggestions`/review material; creating a collection is named as `restructure_execution` (one inline confirmation). Bootstrap and the scaffold teach the advisory, the category and the confirm rule in one clause each — no "remember to use Records" text.
- **Backfill convention.** `describe` gains a state-ledger example with an observation-quality field (`exact|approximate|inferred`) and a `sources` link array; the candidate finding carries the unit references an agent backfills from. No mechanism converts prose into records.
- **Bench, amendment sequence 4.** Two f27-shaped magic-word-free replay families: f28 `collection_promotion_replay` (candidate surfaces within budget, twin stays quiet, ledger end state matches the fold) and f29 `claimed_collection_routing_replay` (a "posted" turn plus artifact lands as an item in the seeded claiming collection, with the false-write dual). Filed as a §7 amendment with a pending receipt; withheld until acknowledged.
- Not in scope: a write-time delta for the candidate sensor (reconcile-time only in v1); schema-evolution suggestions from held items; automatic collection creation under any prominence; model-backed matching; changes to how items are stored.

No model or network call is introduced. Matching is lexical and deterministic; sensors read parsed state the indexing pipeline already produces; every advisory and family fails open.

## Capabilities

### New Capabilities

None. Every behaviour extends an existing capability's contract.

### Modified Capabilities

- `structured-collections`: ADD "Collection manifests may declare and derive claimed domain vocabulary".
- `records`: ADD "Coverage distinguishes complete, partial and blocked ledgers"; ADD "Describe teaches the state-ledger convention".
- `attention-queue`: ADD "Unreflected observations is a structured family derived from collection claims"; ADD "Collection candidacy is a projected audit category resolved by claims"; MODIFY "A structured write settles its own unreflected outcomes" (the record-write delta also settles claimed observations).
- `command-surface`: ADD "Compiled and Evidence writes may return one advisory Records routing hint".
- `agent-bootstrap-contract`: MODIFY "Bootstrap teaches Records routing and boundaries" (routing advisory handling, the candidate category, the collection-creation confirm rule).
- `delegation-envelope`: ADD "Collection creation is restructure execution".
- `epistemic-state-bench`: ADD "Collection-claims replay families f28 and f29".

## Impact

- Code: `structured_collections.py` (claims block), `record_governance.py` (effective claims, coverage), `records.py` (record-write delta hooks), `due_state.py` (second structured family, claims projection, new projection category), `audit.py` (`_check_unreflected_observations`, `_check_collection_candidate`), new `collection_candidate.py` (pure detector and constants), `semantic_writes.py` and the Evidence preserve path (routing advisory), `mutation_terminal.py` (advisory projection), `attention.py`, `review_state.py`, `commands.py` bootstrap text, scaffold references and plugin copy, `benchmarks/epistemic/*`.
- Compatibility: additive. Manifests without `claims` keep working and gain derived claims only. New compact-envelope field `records_routing`; new due-state and audit categories; new attention opt-in category; bootstrap text grows, so the compact byte ceiling must be re-measured and other text trimmed first.
- Dependency: consumes the `coverage` block and `held` count from `repair-records-writer-representation-and-held-records`; archive after it. The f27 false-write dual counts a held file as an extra structured write, which is the correct reading and is why f29 carries no fault-injection arm.
- Governance: every served count, reference and manifest name passes the release filter for the requesting audience; the advisory names only a manifest the caller may read.
