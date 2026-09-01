## Context

The shipped recurrence sensor is deliberately narrow: it counts unresolved body wikilinks on distinct eligible pages, suppresses identities already resolved by a page or active Entity title or alias, and projects an identity-partitioned `entity_recurrence` review item. It is deterministic and read-only, but misses stable identities present only in ordinary prose. Separately, a created Entity can stay sparse while durable contexts remain disconnected, and a vault-defined type's core `parent` is validated but not operational for query or traversal.

The primitive for this change is a recurring stable identity of any kind. People, communities, organisations, places, venues, products, accounts, projects, and future vault-defined kinds all use one evidence and lifecycle contract. The active agent remains the sole semantic decider and the governed curation lane remains the only restructuring executor.

## Goals / Non-Goals

**Goals:**

- Detect bounded deterministic evidence for recurring stable identities in ordinary text while preserving the unresolved-wikilink lane.
- Discriminate reusable identity evidence from raw frequency and measure false positives explicitly.
- Project exactly one identity through promotion, hydration, ambiguity, and closure states.
- Make parent families useful for explicit filtering and Entity traversal without changing leaf storage.
- Give the active agent enough evidence to author a governed plan without adding another semantic decider.

**Non-Goals:**

- General-purpose NER, ontology induction, semantic entailment, confidence scoring, or a background LLM.
- Automatic type registration, Entity creation, editing, relation acceptance, merging, or deletion.
- Making `community` or any other kind the detector primitive.
- Putting entity candidates into due-state or silently admitting f21 into default attention before its existing evidence gate.
- Reinterpreting generic graph `node_types` as entity-family filters.

## Decisions

### D1. Two evidence lanes feed one identity accumulator

The collector merges the compatible unresolved-wikilink lane with an ordinary-text lane restricted to deterministic identity-bearing frames. Both use NFKC plus case-folding for the identity key, retain a deterministic display form, count each eligible page once, and aggregate independent origins rather than occurrences.

The ordinary-text lane is exactly `identity-frames-v1`; it is not an open parser extension point. Its accepted frames are:

1. `typed-copula`: a candidate span at a clause boundary, an exact copula (`is`, `was`, `are`, or `were`), an optional article, and one active registry `cue_noun`;
2. `typed-label`: one active registry `cue_noun`, an exact `:` or em-dash delimiter, and a candidate span ending at the clause boundary;
3. `subject-relation`: `I`, `we`, or an exact title/alias-resolved Entity, followed by one frozen core-v1 predicate and a candidate object span;
4. `identity-relation`: a candidate subject span at a clause boundary, one frozen core-v1 predicate, and an exact title/alias-resolved Entity object; and
5. `body-field`: a Markdown body label from the frozen core-v1 predicate table, `:`, and one or more comma-separated candidate spans. Frontmatter remains excluded.

The frozen predicate table assigns a non-null stable ID to every frame token:

- copulas: `copula.is`, `copula.was`, `copula.are`, and `copula.were` for those exact words;
- typed-label delimiters: `label.colon` for `:` and `label.em_dash` for `—`, with the registry cue bound separately;
- membership relations: `membership.member_of`, `membership.members_of`, `membership.joined`, `membership.belongs_to`, and `membership.belong_to` for `member of`, `members of`, `joined`, `belongs to`, and `belong to`;
- work relations: `work.works_at`, `work.work_at`, `work.works_with`, and `work.work_with` for `works at`, `work at`, `works with`, and `work with`;
- use relations: `use.uses` and `use.use` for `uses` and `use`;
- attendance relations: `attendance.attends` and `attendance.attend` for `attends` and `attend`;
- location relations: `location.lives_in`, `location.live_in`, and `location.based_in` for `lives in`, `live in`, and `based in`;
- commerce relations: `commerce.buys_from` and `commerce.buy_from` for `buys from` and `buy from`;
- stewardship relations: `stewardship.maintains`, `stewardship.maintain`, `stewardship.builds`, `stewardship.build`, `stewardship.organises`, `stewardship.organise`, `stewardship.organizes`, and `stewardship.organize` for the same exact lexical forms; and
- body-field labels: `field.membership` for `member of`, `membership`, or `affiliation`; `field.work_at` for `works at`; `field.work_with` for `works with`; `field.uses` for `uses`; `field.attends` for `attends`; `field.location` for `location` or `based in`; `field.buys_from` for `buys from`; `field.maintains` for `maintains`; `field.builds` for `builds`; and `field.organises` for `organises` or `organizes`.

All lexical comparison is NFKC case-folded, but the table bytes contain the canonical forms and IDs above in sorted order. Inflected or translated forms not listed in the versioned table are unsupported in v1 rather than guessed. The normalized table bytes have a published digest; there is no null, inferred, or implementation-chosen predicate ID.

A candidate span is the maximal adjacent span on the same Markdown clause side of the cue or predicate. It contains 1–8 Unicode letter, number, or mark tokens joined only by single whitespace, hyphen, apostrophe, or ampersand; is 2–128 UTF-8 bytes after NFKC normalization; and ends at `.`, `,`, `;`, `:`, `!`, `?`, a Markdown hard break, or the frame delimiter. Pronoun-only, all-numeric, stopword-only, URL, email, filesystem-path, date/time, inline-code, code-block, Markdown-link-target, and registry-cue-only spans are rejected. Capitalization and script are never span conditions. A clause with an unsupported trailing adjunct is not shortened speculatively; it simply may not recur under the same normalized identity.

The only v1 cue sources are the frozen predicate table, the active entity-type registry's exact normalized `cue_nouns`, and the exact title/alias Entity index used by the two relation frames. Plugins, arbitrary field names, embeddings, statistical NER, and model output cannot add a frame or cue. Every evidence row records the grammar version, predicate ID, predicate-table digest, and registry fingerprint.

Alternative considered: capitalize-and-count proper nouns. Rejected because it is language-biased and creates predictable false positives.

### D2. Independent origins and material facets establish recurrence

An origin is the contributing Source reference where provenance exists, otherwise the stable session or page reference. Derivatives of one Source count once. Ordinary-text evidence requires at least three eligible pages in three independent origins, at least two distinct material facet atoms from at least two origins, and at least one stable-identity cue.

A facet atom is the normalized tuple `(grammar_version, frame_type, predicate_id, cue_or_resolved_entity_ref, clause_skeleton)`. The clause skeleton replaces the candidate span with `<identity>`, normalizes whitespace, and retains only the bounded cue, predicate, and exact resolved-Entity counterpart. Origin and display spelling do not change the atom. Mention-only text, copied boilerplate, naked lists, and repeated copies of one atom do not add material facets. Extraction is deterministic and cannot depend on capitalization, Latin script, embeddings, a generative model, or a float threshold.

For same-label evidence, contexts become a deterministic compatibility graph. Two contexts are connected when they share a facet atom, share an exact resolved co-Entity anchor, or carry registry family cues that are equal or ancestor-compatible. The identity has incompatible clusters only when at least two disconnected components each independently satisfy the complete recurrence gate and their explicit family cues are mutually ancestor-incompatible or their non-empty resolved-anchor sets are disjoint. Small fragments never create ambiguity on their own. Component construction and ordering are insertion-invariant and fingerprinted.

### D3. Payloads contain categorical evidence, not inferred truth

Candidates report identity and display, eligible page/origin/facet totals, bounded context references and excerpts, facet hashes, observed role or membership language, resolved co-occurring Entity refs, active and unresolved type cues, exact/alias/near-match evidence, grammar and predicate-table identity, and evidence and registry fingerprints. Samples are deterministically bounded while full counts remain visible.

### D4. One state machine resolves promotion, hydration, ambiguity, or quiet

Multiple active exact/alias matches or incompatible deterministic clusters produce `ambiguous`; exactly one active Entity produces `hydration` only while qualifying contexts remain disconnected; no active Entity produces `promotion`; otherwise no candidate remains. Near matches and type cues are advisory and never select a target.

### D5. Hydration is connectivity-based

A qualifying context is connected when it contains a canonical body link to the resolved Entity or an accepted graph relation connects it. The substrate does not decide whether prose semantically duplicates a facet. Hydration closes through inspectable connections, removal, or evidence ineligibility, keeping the Entity a compact projection rather than a duplicate fact store.

Hydration work is deterministic batching, not a one-shot truncated sample. A candidate exposes at most eight disconnected contexts, `remaining_disconnected_count`, and a `batch_fingerprint`. One confirmed curation plan may act only on that batch. After its terminal mutation receipt, the active agent may perform exactly one identity-bound hydration recheck; the audit recomputes from the whole corpus and removes connected contexts. After processed batches 1–7, that recheck may return the next deterministic batch with a new signal version and the chain then pauses for a fresh plan and exact confirmation. After processed batch 8, the recheck is closure-only: it reports closed or a bounded `deferred_remaining_count` marker but exposes no ninth batch or plan binding. The chain stops immediately on no terminal mutation receipt, no open hydration candidate, ambiguity or target/registry change, refusal/dismissal, zero remaining contexts, user exit, or that eighth closure-only recheck. Thus at most eight hydration batches are mutated and eight identity rechecks run in one bootstrapped session; any remainder stays visibly open and the next session's ordinary recurrence read resumes it. Every call and session has a fixed work bound, no background loop exists, and successive ordinary sessions can converge an arbitrarily large backlog without the user having to rediscover or re-request the Entity problem.

### D6. Lifecycle identity changes only on material evidence

There is at most one open item per normalized identity. Its signal version binds state, grammar version, predicate-table digest, sorted hashes of all material facets and all disconnected qualifying contexts, resolved targets, material family cues, and registry identity. Redundant mentions, punctuation, ordering, or copies of an existing facet do not reopen a dismissal. A distinct facet, resolution/ambiguity change, target deletion, connection change, completed hydration batch, or relevant registry change does. Creation transitions promotion to hydration when disconnected evidence remains.

### D7. Curation consumes candidates without duplicating authority

The existing curation work-item action accepts the exact review ref and returns current bounded evidence and fingerprints. The active agent may author promotion plans using `create-entity`, hydration plans using guarded `edit` or `accept-relation`, or stop on ambiguity. Unknown type registration remains a separate guarded entity-type registry save; curation cannot write `_Schema` or invent another step kind.

### D8. Parent family is an additive derived view

A core type belongs to its own family. A vault extension with an explicit core `parent` belongs to that parent family; an existing valid extension with no parent belongs to a self-family named by its leaf ID. Parent remains optional and no v1 registry entry becomes invalid. The registry exposes deterministic family identity, members, and matching. Storage retains the singular leaf ID and that leaf's plural folder. Exact leaf filters remain exact; family queries use explicit `page.entity_family`, and traversal uses `entity_type_families` without overloading `node_types`. Results report both leaf and family.

### D9. Attention remains explicit until f21's gate is satisfied

`entity_recurrence` stays registered, selectable, triageable, and addressable by curation, never due-state. It remains outside the unfiltered daily union until the existing calibration and acknowledgement contract authorises admission. The active-agent contract teaches the explicit candidate check; this change does not falsify or rewrite benchmark governance.

Balanced and maximal agents perform at most one ordinary explicit `entity_recurrence` read per bootstrapped conversation or session: during the first user turn handled after successful bootstrap, after the primary task work and before the final response, whether or not the user's topic names entities. They may perform at most one additional general state recheck after an Entity, accepted relation, or entity-type registry mutation that is not already part of hydration continuation. Each ordinary or general read requests one audit pass and at most three candidates.

Hydration continuation is a separate identity-bound budget: after each separately confirmed batch returns a terminal mutation receipt, exactly one recheck may request only that same identity. Rechecks after processed batches 1–7 may return at most the next eight contexts; the recheck after processed batch 8 is closure-only and may return only closed or `deferred_remaining_count`. A newly returned batch authorizes no further call until it receives a fresh exact confirmation and terminal receipt. The session mutates at most eight batches and performs at most eight identity rechecks, leaving any remainder open for the next session's ordinary read. Off and light remain explicit-request-only. If the active surface cannot request an explicit category or attention read, the carrier says the check is unavailable and skips it; it never substitutes a local scan or a model guess.

### D10. Existing authority and curation contracts remain the only write gates

Agent-initiated candidate surfacing and plan proposals are `structural_suggestions`. Unknown-kind registry saves, Entity creation, and curation apply, resume, or compensation are `restructure_execution` and remain confirm-required. Accepted relations are `link_acceptance` and remain confirm-required; when relation acceptance is enclosed by a curation apply, the stricter enclosing `restructure_execution` confirmation governs. No standing delegation cell or candidate-side writer is added.

`add-governed-curation-lane` MUST be implemented, independently reviewed, and merged before work begins on entity-candidate work-item resolution or proposal/apply integration. Detector, registry-family, and carrier work may proceed earlier, but this change cannot claim hydration or promotion execution until that prerequisite ships.

## Risks / Trade-offs

- [Lexical false positives] -> Require independent origins, multiple material facets, a stable cue, and a frequency-matched zero-signal twin.
- [Same-label identities collide] -> Emit ambiguity for multiple matches or incompatible clusters and never choose or merge.
- [Language bias] -> Use Unicode normalization and structural frames, with lowercase and non-Latin fixtures and no capitalization gate.
- [Historical backlog floods attention] -> Retain explicit-category surfacing until calibration permits default admission.
- [Candidate churn defeats triage] -> Exclude redundant mentions and ordering from material signal versions.
- [Family filters silently broaden old queries] -> Add explicit family selectors and retain exact leaf semantics.
- [Curation becomes a second executor] -> Reuse the closed S8 plan and step vocabulary with the same confirmations, receipts, recovery, and compensation.
- [Unsupported prose lowers recall] -> Keep `identity-frames-v1` closed and measurable; add future frames only by versioned contract change rather than guessing.
- [Large hydration sets starve behind truncation] -> Recompute deterministic confirmed batches until zero, binding all disconnected-context hashes into each signal version.

## Migration Plan

1. Preserve existing unresolved-wikilink category and identity partitions.
2. Add ordinary-text evidence and state projection without rewriting Markdown or Entity paths.
3. Add derived family indexes, treating parentless extensions as self-families, and invalidate them when registry identity changes; do not rewrite Entity frontmatter.
4. Keep existing dismissed candidates quiet when their material signal is unchanged.
5. Ship and independently verify `add-governed-curation-lane`, then integrate candidate work items and repeated hydration batches without changing f21's release gate.
6. After this change and governed curation ship, synchronize canonical specs and archive through OpenSpec with strict validation.

Rollback disables the new ordinary-text collector, candidate states, carrier cadence, and derived family indexes. Existing unresolved-wikilink findings, Entities, paths, registry definitions, and any already-confirmed curation receipts remain compatible and are never undone implicitly.

## Open Questions

None. Benchmark calibration staffing is an already-owned external release gate, not an unresolved design decision in this change.
