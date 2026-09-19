## Context

See proposal.md for the failure. The initial inventory skips navigation basenames,
but the content publisher correctly includes those files. Existing preservation
tests explicitly require navigation and primary content in one generation.

## Goals / Non-Goals

Repair fresh and already migrated vaults using the existing publication transaction.
Ordinary recall filtering, policy membership and public mutation authority remain unchanged.
This change does not repair arbitrary missing catalog rows or alter runtime pins.

## Decisions

Remove the navigation-only exclusion from `schema_migration._catalog_items`. Its
existing held reads, membership evaluation and variant construction apply normally.

Projected retrieval must preserve ordinary recall's navigation exclusion while the
canonical catalog includes those rows. Keep the original authorization map for policy
reporting and continuation identity, then derive one retrieval-only selector that
removes navigation selections and adds their identities to its withheld set. This
retains complete catalog coverage without reporting structural exclusions as policy
denials. Use it before lexical/vector/CLIP scoring, graph admission and reranking.
Navigation must never become a seed, target, passage or result. Counterfactual public
REST tests compare every continuation page with navigation absent versus navigation
present and naming an independently withheld target.

Derive a private set of adoption-eligible paths from the actual planned writes in
`catalog_publication`, after normal mutation conversion validates their bindings.
Eligibility requires a navigation basename, a content PathGuard and an exact matching
predecessor hash. Missing catalog rows for these paths are added using the same
replacement-item builder and complete successor namespace as other writes. Preserve
the original planned write and guard through canonical commit. Raw catalog mutation
entrypoints produce no adoption set.

Excluding navigation from publication would violate the existing atomic-generation
contract. Rewriting its expected hash to absence would confuse catalog absence with
canonical-file absence. General missing-predecessor acceptance would hide unrelated
catalog corruption. None is necessary for this migration compatibility repair.

## Risks / Trade-offs

- A stale navigation file could otherwise be overwritten: preserve and exercise the
  canonical content guard, including a change between preparation and commit.
- An incomplete measurement namespace could become active: exercise the existing
  graph/vector closure with a legacy navigation adoption.
- Synthetic catalog setup could hide migration defects again: include a real hosted
  migration-to-serving-to-capture regression using scaffold navigation.
- Navigation names recently edited pages: exclude it before candidate acquisition
  so a public index cannot reveal a restricted page through projected recall.

## Migration Plan

New migrations include navigation immediately. Already migrated vaults repair only
navigation touched by an ordinary valid governed write; older generations remain
immutable. No manual catalog edit or reenrollment is required. Deployment acceptance
must exercise capture with a runtime containing this fix; a green readiness check on
an older immutable image is insufficient.
