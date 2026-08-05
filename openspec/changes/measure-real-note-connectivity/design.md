## Context

The relation-review disposition is satisfied by a narrow, deliberately-chosen set of
typed epistemic edges. That narrowness is correct as a definition of "a typed edge" and
is retained verbatim. What is wrong is using that same predicate as the *only* measure of
whether a page is connected at all, because the two most common real connections — inline
body wikilinks and `sources:` provenance — are structurally invisible to it.

The decisive framing is that this is an **inconsistency between subsystems, not a new
capability**. Verified against a live vault on an active page carrying zero typed
relations and four prose wikilinks:

- the retrieval graph returns four edges, `relation_type: links_to`, `origin: wikilink`,
  and lists all four pages in the seed's neighbourhood;
- the `relation_debt` audit does not flag the page, because it clears on typed relations
  *or* body wikilinks;
- the write-time disposition reports it unsatisfied.

Two of the three already agree. This change brings the third into line with them. It does
not introduce a new edge, relation type, or origin label — all three already exist and are
already emitted by the graph builder. That is also why the risk is contained: the semantics
being adopted are in production, only the consumer is new.

## Goals / Non-Goals

**Goals**

- Stop reporting well-connected pages as relation debt, and stop forcing them through a
  reviewed-none round trip.
- Keep the typed-edge gap visible as a reviewable, non-blocking signal.
- Make reviewed-none rates and reasons observable.

**Non-Goals**

- No minimum relation count, ever. The floor stays at one.
- No change to `qualify_relation`, `_EXCLUDED_FAMILIES`, the supersession exception, or
  the empty-corpus bootstrap.
- No automatic edge creation. Nothing in this change writes a relation.
- No retrieval-ranking effect. Connectivity measurement is a review surface only.

## Decisions

### Two predicates, not one widened predicate

`qualify_relation` stays byte-identical and keeps its meaning: "a deliberate typed
epistemic edge." A sibling `qualify_connectivity` permits excluded families,
non-supersession frontmatter origin, and `wikilink` origin, and resolves targets against
a wider connectable set.

Both call one shared private helper for the structural checks they agree on — authored or
reviewer-accepted, target resolves unambiguously, source endpoint is an eligible governed
page, not self-targeting, registry entry active and scope-valid — so the two cannot drift
apart. Each then applies its own family/origin policy.

Rejected alternative: relaxing `_EXCLUDED_FAMILIES` directly. That would destroy the
distinction between "cited a source" and "refines a prior conclusion", which is the entire
point of the typed vocabulary.

### No new `RelationDisposition.kind` — the single most dangerous edit in this change

`relation_review._commit_plan` dispatches on `disposition.kind` through an `if/elif`
chain whose terminal `else` raises `INVALID_RELATION_REVIEW` ("reviewed-none approval is
required"). A new satisfied kind such as `"connected"` is not matched by any branch, falls
through to that `else`, and **fails every commit it newly applies to** — converting a
connectivity improvement into a write outage.

Therefore the connectivity lane returns `kind="qualifying_relation"` exactly as the typed
lane does, and the distinction is carried in a new `qualifying_signal` field
(`"typed"` | `"connectivity"`). The same hazard exists in `_recovery_result_reviewable`
and in `_DISPOSITION_KINDS`; keeping the kind set frozen avoids all three.

A regression test asserts a connectivity-satisfied commit succeeds and writes a
qualifying receipt, specifically to pin that it never reaches the terminal `else`.

### Connectivity counts outbound edges only

This is a correctness requirement, not a preference. The writer appends a back-reference
into every cited source's `ingested_into:` frontmatter. If inbound edges counted, a note's
own automatically-written back-references — and any Source page that happens to link it —
would satisfy the disposition for free, making the gate vacuous for exactly the pages it
most needs to measure.

The existing scenario requiring an eligible opposite logical endpoint for inbound
relations stays green because of this restriction.

### A separate connectable-target set, not a widened eligible-governed set

`Sources/` pages must be valid connectivity targets — citing a source is a real
connection. The obvious implementation, adding `source` to the eligible-governed types, is
wrong: `eligible_governed_paths` gates the empty-corpus bootstrap disposition, which
requires that set to contain only the page being written. One captured Source in an
otherwise-empty vault would silently destroy the bootstrap carve-out and force a
reviewed-none on a user's very first note.

So `connectable_target_paths` is computed separately, admitting append-only tier and the
`source` type in addition to the eligible set, while reusing every other eligibility rule
(managed governed path, active status, index/log exclusion, hub/snapshot exclusion).

A regression test creates one Source page plus a first compiled note and asserts the
disposition is still `bootstrap`. That test fails if anyone later "simplifies" this by
merging the two sets.

`evidence` is deliberately excluded pending verification that `type: evidence` frontmatter
is consistent across preserved artifacts; `source` only, for now.

### Typed-edge absence is a warning, structurally incapable of blocking

`RELATION_TYPED_EDGE_ABSENT` is emitted at `warning` severity only when
`qualifying_signal == "connectivity"`. Warnings never enter the blocking-findings set, so
this cannot become a write failure through any later refactor of the finding list. It
routes into the existing `semantic_relation_disposition` audit group rather than claiming
a new category, to avoid a second category rename in the same change.

### `missing_sources` is a dedicated optional category, not a required-field entry

Adding `sources` to the per-type required-frontmatter table would be the smaller edit and
is wrong for three reasons:

1. It would emit roughly 880 `warn` findings on the reference vault into a category whose
   job is structural integrity (missing `type`, `status`, `created`), drowning real schema
   breakage.
2. `warn` overstates a chronic, often-honest condition. Provenance-free conclusions from
   live work are legitimate.
3. The auto-fix pass iterates that same required-field table and backfills anything it can
   infer. Provenance is exactly the field that must never be inferred. Keeping `sources`
   out of that table keeps a fabrication hazard from being one commit away.

The dedicated category is `info`, optional, and absent from the default attention set, so
it is opt-in and independently countable, snoozeable, and trackable.

### Reviewed-none becomes visible, not expensive

Reviewed-none reasons are already durably persisted per page and per lifecycle transition,
and already loaded during evaluation — they are simply dropped before reaching any
caller-visible surface. Carrying them on the disposition exposes them across write
feedback, post-hoc findings, and audit metadata with no new storage and no added round
trip.

Reason validation stays free-form. Cold start must remain cheap: a genuinely new topic in
a populated vault has nothing honest to link to, and making the escape hatch expensive
would push agents toward fabricating an edge instead. Visibility, not friction, is the
control.

The disposition census must iterate every evaluation, not only evaluations that produced a
finding — satisfied pages emit no finding and would otherwise vanish from the rate,
reporting a reviewed-none share of 100%.

## Risks / Trade-offs

| Risk | Mitigation |
|---|---|
| New kind falls through `_commit_plan` terminal `else` | Structurally avoided — no new kind. Pinned by a commit-succeeds regression test. |
| Bootstrap carve-out broken by a captured Source | Separate connectable set; pinned by an explicit empty-vault-plus-source test. |
| Inbound back-references make the gate vacuous | Outbound-only; pinned by an anti-vacuity test covering both `ingested_into:` back-refs and inbound Source links. |
| Body-wikilink fact volume degrades write latency | Dedupe by normalized target, per-page cap, computed once during page-state construction with the body already in scope (no re-read). Measured against the governance-overhead and write-latency gates before and after. |
| Reviewed-none submitted for a now-connected page errors | Pre-existing error class, already reachable via typed relations; error text widened to name the connectivity case so the retry path is obvious. |
| Feedback payload shape changes | An excluded-family typed row moves out of rejected-facts and into connectivity satisfaction. This is the intended improvement; covered by a test asserting the new placement. |

## Latency

Recorded before merge, from `scripts/semantic_write_latency.py` at 2k and 8k plus the
governance-overhead gate, run on a quiesced machine. The write-latency gate anchors its
scaling bound to the same run's small-corpus median, so a single noisy run is not
evidence and both runs must come from the same quiesced session.

- Baseline (pre-change): _to be recorded_
- After body-wikilink facts: _to be recorded_

Fallback if the ratio regresses: compute connectivity as a precomputed per-page tuple at
corpus-build time instead of routing through the fact graph. Cheaper, but it loses the
uniform dedupe, identity, and target-resolution reuse the fact path provides.

## Migration

None. Existing pages are untouched: grandfathering exempts pre-existing pages on edit and
post-hoc evaluation never blocks. The newly visible `missing_sources` and typed-edge-absent
signals form a review queue, deliberately not a migration — a bulk repair would fabricate
provenance and manufacture edges, which is the failure this change is designed to avoid.
