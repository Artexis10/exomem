# Design — semantic scope-divergence sensor

Authority order: source+tests > active/accepted OpenSpec > the no-nudge architecture
report (KB, S4 section incl. the red-team's France-detector conditions) > older notes.

## D1 — Evidence source and absence semantics

The only vector source is the existing embedding sidecar table
`semantic_unit_vectors`, read through ONE new read seam:
`EmbeddingIndex.all_semantic_unit_vectors()` — a single paginated corpus-level
SELECT of `(parent_path, unit_ref, source_order, vector)` grouped by
`parent_path`, loaded once per audit sweep. The seam is new because no unit-vector
bulk read exists today: `all_vectors()` is chunk-only (its sole backing query reads
the `chunks` table), and `search_semantic_units` is kNN whose hit type carries no
vector. The write seam (`upsert_semantic_units`) already maintains every row the
accessor reads — no new table, index, sidecar, embedding, or model. A page whose units lack vectors is **not judged** — absence
of evidence produces no advisory, never a fail-closed nag. The audit already owns a
category for vector-invisible pages; this sensor does not duplicate it.

## D2 — Gate composition (v1 preserved, evidence swapped)

A candidate group must clear ALL gates, mirroring v1's convergent-by-construction
rule:

1. **Cohesion**: mean pairwise cosine within the group ≥ φ (the group is one thing).
2. **Separation**: cosine between the group centroid and the page-identity centroid
   ≤ θ (it is not the page's thing). Page-identity centroid = mean of the vectors of
   units NOT in any candidate group, computed after grouping; a page reduced to no
   identity remainder fails the retained-scope gate instead (v1 `MIN_RETAINED_UNITS`).
3. **Mass**: group size ≥ v1 `CLUSTER_MIN_UNITS` (child-note mass, same constant).
4. **Retained scope**: ≥ v1 `MIN_RETAINED_UNITS` units outside all candidate groups.
5. **Declared-identity exclusion**: pages announcing breadth are exempt exactly as in
   v1 (`BREADTH_TAGS` hub/snapshot, navigation basenames, log-shaped pages) — this is
   the twin gate that keeps the deliberate hub and the legitimately heterogeneous log
   quiet.

6. **Term floor**: the group's off-identity recurring terms must number at least
   v1 `CLUSTER_MIN_TERMS`, unconditionally — a divergent geometry with fewer
   distinct recurring terms never fires. This is a deliberate sensitivity cost:
   it keeps partial routing resolving (D5) at the price of missing groups whose
   vocabulary is too thin to label or route.

Grouping is deterministic greedy agglomerative over cosine with a fixed threshold and
stable tie-breaks (unit order = anchor order; no RNG). Pages above a unit-count cap
(`MAX_JUDGED_UNITS`, e.g. 400) are skipped with a named audit note, bounding the
O(units²) pass.

A page whose identity remainder itself reaches candidate mass produces no
finding: both groups become candidates, no retained remainder survives, and the
retained-scope gate rejects the page. This is accepted v1-consistent behaviour
and PROVISIONAL like the thresholds — it bounds false positives on
breadth-shaped pages at the cost of missing a page that has fully bifurcated,
and is revisited with calibration data.

## D3 — Thresholds are provisional product constants

φ, θ, and `MAX_JUDGED_UNITS` are module constants with a PROVISIONAL marker and a
pointer to the calibration posture (they are product constants, not the frozen f20
bench budgets — changing them is a code change, not a §7 amendment). Tests pin
behaviour AT the thresholds with synthetic geometry (vectors constructed to sit just
inside/outside each gate), so the constants can later move without rewriting the
test intent.

Float32 BLAS matmul is not bit-identical across thread counts, so a page
sitting exactly on a gate boundary could decide differently across machines;
findings are keyed on label terms, never floats, so a fingerprint can never
desync — the worst case is a boundary page flickering across hosts, bounded by
the dismissal machinery.

## D4 — Placement and delivery

`audit.py` gains category `scope_divergence_semantic` (corpus sweep; ONE
corpus-level `semantic_unit_vectors` load per audit run via the new accessor,
never a per-page query — the existing parent-path cursor sweep at audit.py:1213
carries no vectors and is unchanged). Findings become review items through the existing
composer — **one fingerprint rule**: fingerprint = page identity + the sorted label
terms of the group (the lifecycle-routing lesson: key the fingerprint exactly as the
audit keys the finding, or dismissals bind to nothing). Material change = label-term
set change. S2 suppression, S6 family dispositions (`registered_families()` gains the
category), first-surfaced ledger and origin tags apply with no new mechanism. The
write path is untouched; no due-state counter family is added (structural advice is
attention-plane, not loop-obligation).

The category registers in `EPISTEMIC_REVIEW_CATEGORIES` (opt-in profile), not
the pinned default attention tuple: that tuple's gate is backlog profile, and a
geometric sensor meeting an existing corpus — where every page it will flag is
already written — starts opt-in.

## D5 — Labels and resolution

Label = top `MAX_CLUSTER_TERMS` recurring normalised terms of the group's units
(v1's `_terms` rule reused). The sidecar rows carry no tags: the audit recovers
each unit's tags from the page's parsed units and joins them to vector rows by
`unit_ref` — vectors supply the geometry, parsed units supply the label
vocabulary; a vector row whose `unit_ref` no longer resolves against the current
parse is dropped from judgment (stale generation), never guessed at. Resolution reuses the resolve-by-state-change rule
verbatim: a group whose label terms are covered (≥2 terms per contributing
destination) by eligible compiled destinations produces no advisory. After
routed terms are subtracted, v1's `CLUSTER_MIN_TERMS` floor applies to the
surviving label — partial routing that leaves fewer surviving terms resolves
the advisory rather than shrinking its label and re-fingerprinting a dismissed
item. This keeps the
15-August France split resolved for the semantic path too — the destinations exist,
so a semantic re-detection of the same material stays silent.

Label terms exclude the page's declared identity terms — v1's own clustering
rule (`terms − identity`) carried over. Without the subtraction the parent
domain's vocabulary dilutes the label, and any destination declaring the parent
domain would cover two terms and silently resolve a genuine divergence.

## D6 — Acceptance fixtures

1. **The 2026-08-29 case, synthesized**: a page whose units share parent-domain
   vocabulary but split into two vector groups (licence-administration vs
   stopping-physics shapes) → advisory fires with `semantic_cluster_diverges`, labels
   name the divergent group's terms. This is red-first: the fixture must FAIL on
   v1-only main.
2. **Synonym-swap survival**: same geometry, vocabulary substituted → advisory still
   fires (the f20 property, provable without the withheld family).
3. **Four twin shapes** with matched frequency/length: bounded-scope (one group ≡
   page identity), tangent (below mass), hub (breadth-declared), heterogeneous log
   (breadth-declared) → all quiet, asserted as zero advisories, not merely absent key.
4. **State-change resolution**: create covering destinations → advisory gone; delete
   one → advisory back.
5. **Disposition + suppression**: family `off` silences it; a dismissed fingerprint
   stays dismissed until the label set changes.
6. **Undeclared heterogeneous page with one cohesive at-mass thread**: mutually
   orthogonal units plus one cohesive group at child-note mass, no breadth
   declared → the finding FIRES, deliberately. A coherent durable thread on a
   scratch page deserves a home; the protected quiet twin is the
   breadth-DECLARED log/hub, not undeclared heterogeneity. Also: staleness —
   a page whose stored vector generation differs from the current parse
   generation is not judged (the material-change contract must never be
   satisfied by deleted text).

## D7 — What the reviewer should attack

The FP surface (twin 4 and real heterogeneous notes), fingerprint stability across
incidental writes, the identity-centroid definition when groups overlap, cost on the
largest real pages, and whether the audit note for skipped pages is honest.

## D8 — Bootstrap clause placement (compact budget)

Measured on the current tree, compact bootstrap is 61,376 bytes against a 61,400
ceiling (`tests/test_bootstrap_compact_budget.py`), and that test's docstring
pre-commits the answer: 24 bytes is not headroom, and the next addition trims or
argues — this change has no claim on the argument. The teaching clause therefore
lands in the FULL contract only. The compact payload stays byte-identical and the
budget test must pass unmodified. Compact carriage of the clause is deferred to
the queued compact-bootstrap trim (route-lifecycle design: "TRIM compact") and is
that work's acceptance concern, recorded here so the deferral is tracked, not
silent.
