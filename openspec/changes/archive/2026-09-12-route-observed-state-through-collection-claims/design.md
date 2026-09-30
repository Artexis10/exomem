# Design — route observed state through collection claims

## Context

See proposal.md for motivation. Facts in the tree (origin/main `b7af6bc0`) that fix the shape:

- Sensors run where their data already is. The lexical scope-divergence sensor (`structure_promotion.py`) is pure over the written page at commit time; the semantic scope sensor and entity recurrence (`audit._check_scope_divergence_semantic`, `_check_entity_recurrence`) run in the audit sweep over `find_module.ParsedPage` lists because "this is where the parsed bodies already are". The write-time corpus context (`SemanticCorpusContext`) carries page frontmatter, titles and projects but not cross-page unit tags.
- The due-state projection (`due_state.py`) has `PROJECTION_CATEGORIES` (served as counts by the S1 carriers on bootstrap, mutating responses and recall), `DELTA_CATEGORIES` (what one write can settle), and exactly one structured-write family, `unreflected_outcomes`, whose docstring says a second structured family needs its own edit rules. `question_aging` is projected but not in the default attention union because its threshold was authored by nobody; `unreflected_outcomes` is in the default union because it fires only on an authored binding.
- `_structure_suggestion_projection` (`mutation_terminal.py`) validates one advisory kind per write, and the command-surface spec confines that advisory's evidence to the written page: it may not name another page. The `due_state` block is the precedent for a separate bounded compact-envelope field.
- Evidence write responses already carry an advisory slot (`semantic_writes.EvidencePreserveResponse`); the sidecar carries `tags`, `title` and a description.
- `record_governance._inspection_observed_values` re-validates the observed free-string vocabulary that inspection already computes per collection (#950). `apply_record_write_delta` has the written record's values in hand and reads one bound Planning snapshot, never the Records collection.
- f27 (`benchmarks/epistemic/journeys/f27_replay.py`, `assertions.py`, `registry.py`, `PREREGISTRATION.md` §7 sequence 3) is the replay harness: two arms, `configure`/`agent_turn`/`snapshot` ops, expectation folded from corpus annotations, store-bearing vocabulary refused at load, paired coverage and false-write dual, families withheld until receipt acknowledgment.
- The preceding change adds `coverage: {committed, held}` to inspection and inventory and stores held candidates under `<collection>/Held/`.

## Goals / Non-Goals

**Goals:** make a collection's contract machine-visible; tell the agent at write time when an observation belongs to a collection; count unreflected observations durably and serve them through the existing carriers; propose a collection when a longitudinal domain recurs unclaimed; prove all of it with magic-word-free replays.

**Non-Goals:** write-time delta for the candidate sensor; automatic collection creation; model-backed matching; multilingual state lexicons beyond a configurable list; schema-evolution advice from held items; any change to item storage; projector redesign.

## Decisions

### D1 — Claims: declared plus derived, maintained as a projection

`claims` is an optional manifest block following the `record_presentation` pattern (`_claims_json_schema`, a `CollectionManifest.claims` field, `_parse_claims`): `tags`, `terms`, `entity_types`, `evidence_kinds`, each a list of at most 24 strings, normalised with the same `_terms` rule the lexical sensor uses (NFKC, casefold, punctuation split). Effective claims = declared ∪ derived. Derived claims come from the collection's own items: observed values of enum fields; observed values of string fields with at most `MAX_DERIVED_VALUES` (12) distinct values (this excludes free text such as exact quotes and notes by construction); and values of a declared array-of-string `tags` field. Derived claims are a projection kept in the due-state state file under `claims[<manifest path>]`, rebuilt by reconcile from the same census inspection performs, and folded incrementally by `apply_record_write_delta` from the written values (no Records re-read, the same posture as the outcomes delta). A collection with fewer than `MIN_CLAIM_TERMS` (2) effective terms, or with `lifecycle` other than active, is not a routing target.

*Why derived at all:* thirteen live collections predate this change; declared-only would leave every one of them passive until edited. *Why a projection:* computing observed values for every collection on every note write is the cost the outcomes delta was redesigned to avoid. *Rejected:* deriving claims from item free text (floods claims with one-off words); model-based matching (banned by the constitution and unnecessary for vocabulary overlap).

### D2 — Routing advisory `records_routing`, a sibling field, not a suggestion kind

On compiled-note writes (`remember`, `observe_memory`, `edit_memory`, `replace_memory`) and Evidence preserves, after the guarded write returns, compute the written page's terms: page tags, unit tags and title terms for a note; sidecar tags, title and description terms for Evidence. For every routing-target collection the caller may read, coverage = |terms ∩ effective claims|. Emit an advisory only when exactly one collection has coverage ≥ `MIN_CLAIM_COVERAGE` (2) and strictly more than every other; a tie or no candidate emits nothing. Payload: `collection` (manifest path), `title`, `matched_terms` (≤ 6, sorted), `natural_key` (declared field names), `strength` — `strong` when the page also carries a value shaped like a natural-key field (a term equal to an observed natural-key value, or a value matching a key field's declared type such as a date or URL), else `moderate`. Projected by `mutation_terminal` as a bounded top-level compact field with its own validation, exactly as the `due_state` block is.

*Why not a `structure_suggestion` kind:* the command-surface contract confines that channel's evidence to the written page, and the advisory's whole point is to name another page; a sibling field carries its own disclosure rule (only a manifest the audience may read) without weakening the existing one, and avoids the one-slot conflict with scope divergence. Fail-open, never affects the terminal.

### D3 — `unreflected_observations`, the second structured family, with its own edit rules

Entry identity is `(collection_id, observation_ref)` where `observation_ref` is the written page's canonical reference; one entry per collection and page, stored in the projection under pages keyed by the manifest path so the record-write delta finds them by collection.

- **Add:** the D2 computation, when it emits, adds or refreshes the entry with `matched_terms`, `observed_at`, `page_path`.
- **Settle (record write):** `apply_record_write_delta` for the claiming collection drops entries whose page the written record links (any `link`-typed field or `sources` array resolving to the page) or whose `matched_terms` contain a written natural-key value. It touches only its own collection's entries. Settlement is audience-relative: the shared projection retains the observation and its bounded reflecting-record support, suppressing the served entry only when that audience can read a valid reflector. A returned artifact path, its declared governance companion page and that page's stable reference address the same evidence; the projection retains declared aliases rather than guessing filenames.
- **Heal (reconcile):** `_check_unreflected_observations` recomputes from Evidence sidecars and compiled pages modified within `OBSERVATION_LOOKBACK_DAYS` (90) against current effective claims. The lookback limits discovery of untracked pages; already tracked unresolved observations remain eligible regardless of age. An entry whose page is gone, or whose collection no longer covers ≥ `MIN_CLAIM_COVERAGE` of its terms, disappears — resolution by state change, no dismissal memory.
- **Grace:** entries younger than `OBSERVATION_GRACE_HOURS` (24) are stored as pending with a due date, like a prediction window, and served only afterwards; an episode still in progress is not a gap.
- **Fingerprint:** `sha(collection_id, observation_ref, sorted matched_terms)`; a re-write adding matched terms resurfaces a dismissed entry under the existing material-change rule.
- **Registration:** `PROJECTION_CATEGORIES`, `DELTA_CATEGORIES` (structured, so `STRUCTURED_DELTA_CATEGORIES` becomes two and the outcomes-family docstring's promise is honoured with explicit per-family rules), `DEFAULT_ATTENTION_CATEGORIES` (like `unreflected_outcomes`, it fires only on authored or derived claims that a person can see and edit), `audit.ALL_CATEGORIES`, dispositions derive. Served by `block_for_structured_write`, the S1 carriers and `review_memory(mode="attention")`.
- **Coverage:** `record_memory(inspect)` coverage gains `unreflected` (count and ≤ 20 refs); inventory gains the count. Complete = no held, no unreflected; partial = unreflected > 0; blocked = held > 0.
- **Disclosure:** an entry is served only when the audience may read both the page and the manifest; the served view is recomposed at serve, as for outcomes.

All constants are PROVISIONAL in one module and named in the spec as such.

### D4 — `collection_candidate`, an audit category resolved by claims

A pure detector module `collection_candidate.py` over a units table `(page, unit_ref, terms, date, text)` built from `ParsedPage`s in `_check_collection_candidate(vault_root, pages)`, governance-bounded by the same release filter the other sweeps use. For each term not in `BREADTH_TAGS`, not a project key or core epistemic category, and not covered by any collection's effective claims: gates `SPREAD_MIN_PAGES` (3 distinct pages), `DATES_MIN` (3 distinct dates) spanning `SPAN_MIN_DAYS` (14), `STATE_UNITS_MIN` (3 units carrying a state-change lexeme from a closed default list, a currency amount, or an ISO date), `IDENTITIES_MIN` (2 co-recurring non-lexicon terms). All PROVISIONAL. One finding per qualifying term: `review_partition` = term, `detail` in domain language, `meta.evidence_units` (≤ 8 unit refs), `meta.domain_terms` (≤ 6), `strength` strong when spread or state units reach twice their thresholds, else moderate, `signal_version` = sha(sorted evidence unit refs) so new units resurface a dismissed candidate. Registered in `audit.ALL_CATEGORIES`, `EPISTEMIC_REVIEW_CATEGORIES` (opt-in to the attention union, exactly the `question_aging` precedent: a threshold nobody authored) and `PROJECTION_CATEGORIES` as a recompute-only category (the `supersession_integrity` precedent), so its count rides the carriers to hookless clients. Reconcile-time only in v1; the write path does not maintain it.

*Why audit-time:* the evidence is cross-page and lives in the parsed units, which the sweep already holds; a write-time version would need an index query per tag of every compiled write. *Why lexical:* vocabulary recurrence across pages and dates is what the dogfood case is made of (`#subscriptions`, `#ai-accounts`, `#claude-max-20x` across finance, strategy and incident notes); the semantic counterpart is an open question, not a v1 need. *Rejected:* firing from a single note's units (that is scope divergence, already shipped); an LLM classifier (banned); default-union membership (unauthored threshold).

### D5 — Authority

The advisory and the candidate are advisory material under `structural_suggestions`; an append into a claiming collection is `proactive_capture` and follows the served disposition; creating a collection is `restructure_execution`, confirm-required, named explicitly in the envelope spec. The agent flow bootstrap teaches: on a strong candidate, draft the schema through `record_memory(describe)` and `validate`, ask one question in domain language, create on yes, then backfill from the evidence units. No new envelope cell; standing delegation remains refused.

### D6 — Backfill is a convention, not a mechanism

`describe` gains a state-ledger example: a natural key of identity plus effective date, a status enum, an `observed_precision` enum (`exact`, `approximate`, `inferred`) and a `sources` link array. The candidate finding's `evidence_units` are the agent's backfill inputs; only exactly dated units become items, each citing its unit reference in `sources`; prose notes are never deleted.

### D7 — Contract text stays small

Bootstrap: one clause on `records_routing` (route the observation into the named collection when the disposition allows, resume a held candidate if one exists, never append from the advisory alone without the observation), one naming the `collection_candidate` category and the confirm rule. The compact profile has roughly 100 bytes of headroom, so equivalent bytes are trimmed elsewhere first and both sizes are recorded. Scaffold `planning-records.md` gains one paragraph; the plugin copy stays md5-identical.

### D8 — Bench sequence 4

f28 `collection_promotion_replay`: an authored multi-session episode in a generic ledger domain (tool licences for a small studio) whose turns report purchases, cancellations, billing moves and refunds across dates, with tentative and elapsed-time distractors; a frequency-matched twin with one-off purchases. Assertions: `collection_candidate_surfaced_within_budget` (a `collection_candidate` finding naming the domain is present in the projected attention or due-state surface by the annotated turn, and composes `signal_absence_checked_across_all_surfaces` for the twin) and `ledger_state_matches_expectation` (after the agent creates the collection on the scripted confirmation turn, the projected collection claims the domain and holds one item per annotated event with matching natural keys and fields; tentative turns land nothing). f29 `claimed_collection_routing_replay`: a seeded claiming collection; turns report publications with final text and an artifact; assertions `claimed_observation_reflected` (an item per annotated publication with the natural key, `sources` linking the preserved artifact) and the existing `no_structured_write_beyond_expectation`. Both operational journeys, both arms, existing operation vocabulary, corpus builders as pure functions, store-bearing vocabulary refused at load with `collection`, `ledger`, `schema`, `claims` and `track` added to the refused list for these corpora. §7 amendment sequence 4 with a pending receipt; registry mirror `f28 → 4`, `f29 → 4`; `COMPOSES_ABSENCE_META` and `REQUIRES_SUBJECT` gain the f28 quiet assertion; no catastrophic addition; no operation kind; a projector field only if the attention surface is not already projected, additive and version-bumped as sequence 3 did.

## Risks / Trade-offs

- [Derived claims pull generic values (e.g. `other`, `unknown`) into claims and over-match] → derived values pass the same `BREADTH_TAGS` exclusion, require ≥ 2 items carrying the value, and the advisory needs a strict-winner coverage of 2; fixtures include a generic-value twin.
- [Two collections claim overlapping vocabulary] → ties emit nothing; the agent still has inventory; inspection can show effective claims so a user can disambiguate by declaring `claims`.
- [Grace window hides a gap the user asks about immediately] → `record_memory(inspect)` reports pending entries too, under `coverage.pending`; only the carriers wait.
- [Candidate sensor is English-lexicon bound] → currency amounts and ISO dates are script-neutral; the lexeme list is a module constant documented as PROVISIONAL and overridable in a later change; f28's twin is frequency-matched so a lexicon miss reads as a miss, not silence.
- [Compact bootstrap ceiling] → trim before adding; measure; the compact profile must not exceed the pinned ceiling.
- [Second structured family corrupts the first's entries] → separate page buckets keyed by family, separate composers, mutation-removal probe per family.
- [Held files count as extras in the f27 dual] → intended: a refused write is not the expert end state; f29 has no fault arm.

## Migration Plan

Additive. Deploy through the existing local and hosted paths. Existing collections gain derived claims on the first reconcile after deploy; no manifest edits are required. Rollback is a revert; the projection sections for the new categories are dropped on the next reconcile. Bench families remain withheld until the sequence-4 receipt is acknowledged.

## Open Questions

Deferrable without changing specs or tasks: exact PROVISIONAL constants after the first live reconcile over a real vault; whether the candidate sensor later gains a write-time delta keyed on the written page's tags; whether derived claims should require a minimum item count above two.
