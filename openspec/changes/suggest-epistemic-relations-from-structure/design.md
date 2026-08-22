## Context

`suggest_relations` composes four generators and truncates the deduplicated
result at `limit` (default 10). Three are deterministic (`wikilink`,
`frontmatter_sources`, `shared_sources`) and one is optional
(`embedding_proximity`). `relation_queue._page_candidates` consumes
`suggest_relations` directly, so anything added here reaches the acceptance
queue with no queue changes at all.

The graph sidecar already materializes everything the new generators need.
`graph_nodes` carries `unit_ref`, `unit_category` and `unit_kind` for every
addressable semantic unit; `graph_edges` carries `relation_type`,
`raw_relation`, `registry_status`, `origin` and `source_path` for every edge,
including block-level ones. No schema change, no `SCHEMA_VERSION` bump, and no
rebuild is required.

## Goals / Non-Goals

**Goals:**

- Make epistemic-family relation suggestions structurally possible without
  inferring any meaning the author did not write.
- Read the sidecar once per page across all three generators, and soft-fail to
  no candidates when the snapshot is unavailable.
- Keep every candidate's evidence sufficient to re-derive its identity, so a
  dismissal expires when the evidence genuinely changes.
- Leave the vault, the graph edges, the acceptance queue, the command registry
  and the pinned tool surface untouched.

**Non-Goals:**

- Unit-level (fragment) relation targets — deferred to
  `resolve-relation-fragment-targets`, which owns the schema bump and the
  write-path latency evidence.
- Ranking, scoring, confidence, or any ordering beyond deterministic sorting.
- Any model-backed path.

## Decisions

### The seam: two authoring forms behave oppositely

A `- relations: k: [[T]]` metadata row on a rich semantic block produces an edge
whose `src_key` is the **block** key, with `origin = 'semantic_relation'`, and
**no** page-level edge. A plain `- k [[T]]` bullet in the same block's body is
parsed by the note-level relation scanner and produces a **page-level** edge
whose `src_key` is the file key, and never appears in `unit.relations`.

The lift therefore selects `origin = 'semantic_relation' AND src_key <> file_key`,
which admits exactly the first form and excludes the second by construction. A
`NOT EXISTS` against a page-level edge with the same `relation_type` and target
drops any unit relation the author has already promoted by hand.

This is the one seam the whole generator rests on, so a regression test asserts
both directions rather than only the positive case.

### Question units land on two indexed axes, so UNION rather than OR

A rich `## Open Question` has `unit_kind = 'open_question'`, but a `- category:`
metadata row overrides its category. A compact `- [question]` has
`unit_kind = 'observation'` and `unit_category = 'question'`. A predicate on
either column alone misses cases.

The two columns are served by two separate indexes
(`idx_graph_nodes_unit_kind`, `idx_graph_nodes_unit_category_kind`), and an `OR`
across them defeats both — the same reasoning already recorded beside the
relation-type indexes for relation-filtered recall. The query therefore UNIONs
two indexed branches on each side of the join.

### Question normalization lives in SQL, on both sides

Normalization is `trim(rtrim(lower(trim(text)), '?'))`, applied in SQL on both
sides of the join so no Python normalizer can drift from it. Two deliberate
recall limits follow, and both are asserted rather than left implicit: SQLite's
`lower()` is ASCII-only, so a non-ASCII case difference is not normalized away;
and only trailing question marks are stripped.

### Result-adjacency is defined over the resolution graph

"Both pages hold a `result` unit" would fire on nearly every compiled note and
flood the queue. Adjacency is instead defined as: each page carries a
**unit-level** `answers` or `resolves` edge to the *same* target — competing or
complementary answers to one thing. That is a real shared observation and
mirrors `shared_sources` directly.

### The lift proposes the authored label, normalized, gated by family

The proposed relation type is the `raw_relation` recorded on the unit edge — the
label the author actually typed — put through `relation_registry.normalize_relation`.
The generator cannot manufacture a meaning, only fail to promote one.

Normalizing is not cosmetic. `- relations: Answers: [[T]]` parses with no
diagnostic and resolves to `registry_status='core'`, but the canonical
relation-bullet grammar in `markdown_relations` accepts only
`[a-z][a-z0-9_.-]{1,80}`. Proposing the label verbatim therefore authors
`- Answers [[T]]`, and the governed accept refuses it with
`SEMANTIC_CONTRACT_BLOCKED: malformed_relation` — naming nothing the reader can
act on, and leaving a queue item that is permanently unacceptable and reappears
on every `build_queue`. The same holds for `EVIDENCED BY:` and `evidenced by:`.
`normalize_relation` is the same function the registry used to resolve the edge
in the first place, so the proposal is still the authored kind.

The family allowlist (`answer`, `resolution`, `question`, `support`,
`contradiction`, `refinement`, `evidence`, `duplication`) is resolved through
the registry at call time, so a vault extension kind parented into an allowed
family lifts without a code change. Causality is deliberately excluded:
promoting one unit's `causes` claim to the whole page would assert a mechanism
between the *pages* that nobody wrote.

`registry_status` is filtered to `core`, `alias` and `extension` separately from
the family gate, because a family can admit a deprecated, scope-violating or
unregistered kind that the writer would reject on accept.

### Evidence is fingerprint-load-bearing

`relation_queue._evidence_signal_version` hashes
`{page_signal_version, method, relation_type, to, evidence}`. A candidate driven
by a *different* page whose evidence omitted that page's identity would keep the
same fingerprint through any later edit to that page, so a dismissal would
become permanent.

Both co-participation generators are exactly that class, so their evidence
carries the other side's `unit_ref`, anchor, and the relation kinds it used. A
regression test dismisses a `shared_open_question` candidate, re-anchors the
*other* page's question while this page stays byte-identical, and requires the
candidate to reappear with a new fingerprint.

### Aggregate within the generator, not only across generators

`_dedupe_candidates` keys on `(from, to, relation_type, method)` and excludes
`evidence`, so it collapses duplicates *within* a generator too. A naive
one-row-per-match emitter would silently drop every match after the first. Each
generator therefore aggregates to one candidate per `(target, relation type)`
and folds all matches into a list inside `evidence`. The three method names are
fresh, so they cannot collide with the existing four either.

### Registration order is load-bearing, and structural goes first

`_wikilink_candidates` is unbounded and `suggest_relations` truncates at `limit`
(10 by default in the acceptance queue), so ordering is a budget rather than a
presentation choice.

An earlier revision of this design placed the structural generators after the
three deterministic ones, reasoning that the deterministic candidates were cheap
and already relied upon. **That reasoning ran backwards and has been reversed.**
A page with twelve body wikilinks and one typed unit relation yielded zero
structural candidates — and a dense compiled note is exactly the page that
carries typed unit relations, so the change would have been silent on its own
motivating case. The interaction is worse than simple truncation: wikilink
candidates are generated before `relation_queue._classify_candidate` drops the
already-authored ones, so the budget can be spent on candidates that are then
discarded.

The ranking that follows: an author-written typed unit relation is the
highest-evidence signal in the set, and a body wikilink is the lowest-cost to
regenerate on the next read. Structural generators therefore run first, and the
existing four keep their order relative to one another. Pinned in both
directions, plus a dedicated link-heavy-page regression.

The residual risk is the mirror of the one being fixed: the three structural
generators are capped at three candidates each, so on a page rich in both
semantic units and wikilinks they can occupy nine of ten slots. That is bounded
by construction, it can only happen on a page whose author has written a great
deal of structure, and the displaced `links_to` candidates are regenerated on
the next read once these are accepted or dismissed.

### Bounds

One SQL statement per generator, each with a row `LIMIT`; at most three
candidates per generator per page; at most five folded matches inside one
candidate's evidence, with the true total reported alongside. A test proves the
query count is constant between a two-page corpus and a 201-page corpus.

### The two co-participation generators suppress each other's duplicates

Pages that share an open question very often also answer the same target, so
`shared_open_question` and `shared_resolution_target` frequently find the same
peer — and both propose `relates_to`, meaning the identical bullet. Since
`_dedupe_candidates` keys on `method`, both would survive and spend two of ten
slots on one edge that can be accepted only once (after which the second is
filtered as an authored edge anyway). `_structural_candidates` therefore drops a
later structural candidate whose `(target, relation type)` an earlier one has
already claimed. The surviving candidate still carries the peer's unit identity,
so dismissal semantics for the edge that is actually proposed are unaffected.

## Risks / Trade-offs

- **The causality exclusion rests on a stated principle, not on a count.** The
  allowlisted families express an epistemic stance between bodies of knowledge,
  which a page can hold as a whole; causality asserts a mechanism between the
  things described, which is a property of the referents, not the documents.
  Broadening a stance yields a reviewable statement; broadening a mechanism
  relocates it onto subjects that cannot bear it. Widening the allowlist to
  causality would need a different argument, not one more entry.
- **Page-level targets force `relates_to`.** "Both pages hold the same question"
  looks like it licenses `duplicates`, but the accepted bullet would read
  `- duplicates [[B]]` and assert that the *pages* duplicate. This is the
  concrete cost of deferring fragment targets; revisit `shared_open_question` as
  a `duplicates` proposal once `to` can address a unit.
- **One extra sidecar read per page.** `relation_queue.build_queue` runs
  `suggest_relations` for up to 50 pages, each already opening one snapshot for
  `shared_sources`. Sharing one snapshot across all three new generators holds
  the increase to one additional validated open per page rather than three.
- **Truncation interaction.** On a page with more than `limit` wikilinks, no
  structural candidate is reached. Accepted; the alternative — registering
  before the deterministic generators — would displace suggestions users already
  depend on.

## Migration Plan

None. No schema version change, no sidecar rebuild, no vault write, and no
change to any persisted state. The generators are additive read-only paths
inside an existing propose-only operation.

## Open Questions

None blocking. `shared_open_question` should be revisited as a `duplicates`
proposal once relation targets can address a unit, which is tracked by the
separately-filed `resolve-relation-fragment-targets`.
