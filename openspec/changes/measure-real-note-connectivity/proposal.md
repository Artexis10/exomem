## Why

The relation-review disposition works, but it measures a surface narrower than the
connectivity it is meant to protect, and compiled notes decayed on every axis it cannot
see.

Measured over 1,372 compiled notes in a live vault, by `created:` week, applying the same
exclusions `relation_debt` applies (hub/snapshot tags, `-architecture`/`-snapshot` stems,
inactive statuses):

| cohort | n | median body wikilinks | zero body wikilinks | ≥1 typed relation | fully disconnected |
|---|---|---|---|---|---|
| June | 219 | 3.0 | 6% | 2% | 6% |
| Jul W1 | 310 | 2.0 | 39% | 1% | 39% |
| Jul W3 (post-gate) | 98 | 2.0 | 37% | 53% | 36% |
| Jul W4 | 98 | 2.0 | 19% | 52% | 19% |

The disposition raised typed-relation coverage from 1% to 53% — it did the job it was
specified to do. But body-wikilink connectivity had already collapsed (6% → 39%
zero-link) and the disposition could not register the recovery of that surface, because
a qualifying relation excludes exactly the connections most notes actually carry:

- Body wikilinks emit no relation fact at all, so a note with four resolved inline links
  to governed pages is indistinguishable from an empty note.
- `sources:` provenance is rejected three separate ways — excluded family `derivation`,
  non-supersession frontmatter origin, and an ineligible append-only target.

The consequence is not merely a reporting gap. A genuinely well-connected note is
reported as relation debt and is pushed through a reviewed-none round trip to commit.
That is friction applied to the correct behaviour, and it teaches an agent that the
cheapest compliant note is one typed row and nothing else.

The fix is to widen what the disposition can *see* while leaving what counts as a
deliberate typed epistemic edge exactly as specified. No floor is raised, no quota is
introduced, and honest zero stays cheap: 71% of connected notes already carry more than
one relation voluntarily, so a minimum count would only manufacture edges — the failure
`_Schema/references/audit-checks.md` explicitly warns against.

## What Changes

- Add a second, explicitly weaker connectivity predicate beside the typed-relation
  predicate. The typed predicate is unchanged. Resolved outbound body wikilinks and
  non-empty `sources:` to connectable governed pages satisfy the disposition when no
  typed edge exists, and the disposition reports which signal satisfied it.
- Emit `wikilink`-origin relation facts from body links, deduplicated by normalized
  target and capped per page. The `links_to` registry entry already permits this origin.
- Add a connectable-target set (governed compiled types, entities, and append-only
  `Sources/`) kept separate from the eligible-governed set, so the empty-corpus bootstrap
  disposition is unaffected.
- Report a non-blocking `RELATION_TYPED_EDGE_ABSENT` warning whenever connectivity — not
  a typed edge — satisfied the disposition, so the typed-edge gap stays visible and
  reviewable without blocking a write.
- Surface reviewed-none reasons, which are already persisted but discarded before they
  reach any caller-visible surface, and report a disposition-kind census over every
  evaluated page rather than only pages that produced a finding.
- Add a non-blocking empty-`sources:` warning for the compiled types whose frontmatter
  spec marks provenance required, plus a dedicated optional `missing_sources` review
  category. Neither blocks a write.
- Restore the `remember` documentation for `sources:` — its wikilink form and the
  `ingested_into:` back-reference mechanic — which the product-surface redesign dropped.

## Capabilities

### Modified Capabilities

- `semantic-write-contract`: The relation-review disposition gains a second, weaker
  connectivity satisfier reported distinctly from a typed edge, plus a non-blocking
  typed-edge-absent warning. The qualifying-relation definition, the excluded families,
  the supersession exception, and the empty-corpus bootstrap are unchanged.
- `attention-queue`: Adds optional, non-default, informational `missing_sources` and
  `relation_review_debt` measurement categories and a disposition-kind census.
- `command-surface`: `remember` documents the `sources:` wikilink form and its
  `ingested_into:` graph effect, and returns a warning rather than an error when a
  compiled type that requires provenance omits it.

## Impact

- Extends `semantic_contract`, `activation`, `relation_review` result plumbing,
  `semantic_writes` feedback, `note` warnings, and `audit` categories. No new module,
  dependency, model, background process, or index.
- Body-wikilink fact derivation is the only new per-write work. It is bounded by a
  per-page cap and reuses the existing fence-aware wikilink scanner and target resolver.
  Latency is re-measured against the governance-overhead and write-latency gates before
  and after, and the numbers are recorded in `design.md`.
- Pure-substrate: every added signal is deterministic Markdown and frontmatter
  measurement. No model, no score, no confidence float. The typed-edge gap is surfaced
  for review and never auto-repaired, auto-decayed, or used to rank retrieval.
- Existing pages are untouched. Grandfathering already exempts pre-existing pages on
  edit, and post-hoc evaluation never blocks, so the newly visible signals form a review
  queue rather than a migration.
- One behaviour change requires a caller-visible note: a reviewed-none decision submitted
  for a page that now satisfies via connectivity is reported not-applicable. This error
  class already exists for typed relations; widening satisfaction moves more pages into
  the no-review-needed branch rather than creating a new failure mode.
