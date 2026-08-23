# Design: flag-superseded-plan-motivation

## Context

`plan_progress.py` is a read-only reader over two shipped profiles. It measures
and refuses to reason: no `health`, no ratio, no ranking, no triageable
reference, and a vault byte-identical afterwards. Everything below is
constrained by keeping those properties true while the reader learns to look at
one more field.

## The binding constraint: one indistinguishable outcome

`_observe`'s docstring already states the rule for Records evidence — *a
missing target and a withheld target return the same reason so the review
cannot be used to probe for hidden collections* — and a shipped test pins it.
Memory references reach further than collection references, into ordinary
knowledge pages, so the rule has to hold across more failures.

Five distinct failures collapse into `motivation_unavailable`:

| Failure | Mechanism |
|---|---|
| Reference not held | the identity resolves to no page |
| Reference held twice | the identity resolves to two pages |
| Malformed reference | the identity does not parse |
| Blocked page | a governance decision below full release |
| Excluded page | the `excluded` access tier, which governance never sees |

None of them emits a path, a title, a page count, a successor, or a counter of
its own. The tally counts every unresolved entry under one key.

Three consequences fall out of that, and each is load-bearing:

**A malformed reference is kept during normalization, not dropped.** Dropping
it would make invalid input the one case a caller could tell apart — by
counting the entries that came back. Only a value that is not a bounded
non-empty string is dropped, exactly as an unusable evidence descriptor is.

**Authorization precedes uniqueness.** An earlier draft of this change decided
uniqueness over the *unfiltered* resolution, reasoning that filtering first
would drop an unreleased twin and present a duplicated identity as a confident
unique resolution. That reasoning was wrong, and measurement showed it
backwards.

Under the unfiltered ordering the identical reference **resolves** when no
hidden page shares the identity and **refuses** when one does — so the presence
of an unreleased page is directly observable. Under filter-first the two cases
are indistinguishable. The bound claimed for the residual was also too
generous: it named a reader who had authored *two* pages sharing an identity,
when only one is needed — the other is the hidden page being probed. `exomem_id`
is ordinary frontmatter, so anyone who has ever seen an id (released once and
later restricted, or read from an export or a receipt) can author a single
released page carrying it and obtain a clean resolved/unresolved oracle.

So the ordering bought tidier semantics and paid for it in disclosure, in the
one module whose stated purpose is not disclosing. Filtering first answers about
the pages this reader may actually see, which is the honest answer as well as
the silent one. A duplicated identity remains an owner-visible repair item
through `backfill_ids(dry_run=True)` and `issues()`, which is where it belongs.

**The work performed is a function of the batch, never of what it found.**
The batch resolver takes one whole-corpus scan, unconditionally. A sidecar fast
path was written first and removed: consulting the sidecar and scanning only for
the ids it could not answer makes the *work performed* a disclosure channel —
responses stay byte-identical while a caller times the call and learns whether
some page, released or not, carries the cited id, with no authoring prerequisite
at all. Scanning only when no sidecar exists closes that channel but reads any
page written outside a governed write as absent, which is most of an ordinary
vault; measured, it left every reference in the supersession suite unresolved.
So the resolver pays one walk per call and buys a property rather than a
latency: how many well-formed ids were asked for is caller-visible already, and
nothing else moves.

**The budget verdict is computed before any target is consulted.** It comes
from a counter over the retained items' distinct authored references, so
`motivation_budget_exhausted` cannot vary with what exists.

## Why a new batch primitive rather than the two that exist

`ReferenceIndex.resolve` and `resolve_identifier_read_only` are both wrong
here. Each costs a corpus scan per reference, and each raises
`AMBIGUOUS_REFERENCE` — whose message states in how many pages the identity
appears. A caller forbidden to disclose that count cannot afford to catch it.
`resolve_identifier_read_only` additionally returns unrecognized input
unchanged with no existence check, so a non-reference would pass straight
through as a path.

`memory_refs.paths_for_ids_read_only(vault_root, ids)` sits beside the existing
reverse-direction batch `refs_for_paths`: **one** `_scan_pages` pass for the
whole batch, every path per identity, and nothing raised. Keeping it in `memory_refs` also
keeps corpus-walking code out of `plan_progress.py`, whose source a shipped test
greps for mutation calls.

It creates and rebuilds nothing, which matters more than it looks: `.refs.sqlite`
is registered internal state, so the canonical byte census skips it and an
accidental `rebuild_all()` inside a read is invisible to both shipped
write-guard tests. This change asserts the sidecar's bytes directly, and greps
this module's source for `rebuild_all`, `ReferenceIndex(`, `refresh_paths`,
`upsert_after_write`, and `resolve_identifier(` — the paren mattering, because
the name is a substring of the read-only variant.

## Supersession is read from status alone

`find_corpus.CACHE` → `ParsedPage.status == "superseded"`, and nothing else. A
non-empty `superseded_by` is also true of a hand-edited page whose status was
never flipped; treating that as supersession would report an inconsistency as a
fact. Reporting the inconsistency is the audit surface's job.

## Counts, not flags, and a constrained signature

`type(True) is int` is `False`, and the shipped suite asserts exactly that over
every divergence value, so all four additions are counts. The invariants worth
naming: `motivation_refs == motivation_resolved + motivation_unresolved`, and
`motivation_superseded <= motivation_resolved`. A reference skipped for budget
counts as unresolved, which keeps the first invariant true through truncation
and keeps a skipped reference indistinguishable from a missing one at the count
level as well.

`divergence()`'s signature is constrained by shipped tests in two directions at
once. They call it with a single positional argument, *and* they assert the
returned dict by equality against exactly seven keys. So the motivation counts
arrive through an optional second parameter that defaults to absent rather than
empty: omitted, the result is the seven-key block it has always been; supplied
— including as an empty sequence — the four counts appear. `review()` always
supplies it, so every reviewed item carries all eleven.

## Projection gates on the predicate, not on declaration

`_planning_page` projects `motivation` only when
`planning.motivation_is_governed(manifest)` is true. The obvious
`name in manifest.schema.fields` is not equivalent: the predicate additionally
requires `field.type == "array"`, which is exactly the legacy free-text case it
exists to exclude. Reading prose as references would invent counts out of a
sentence.

No reviewed output separates the two gates, and that was measured rather than
assumed. Under a string-typed `motivation` the projected value is a string,
which `motivation_refs` drops on shape alone; a list hand-edited under that same
string-typed field refuses the whole collection inside `planning.query` before
the review sees a row. Swapping the predicate for the declaration check passes
both review tests. So the gate is defence in depth — kept because the narrower
predicate is the correct one to write and the equivalence is a property of two
other layers rather than of this one — and it is pinned directly at the
predicate, where the difference exists, instead of through a review assertion
that would pass either way.

One thing follows from that gate which is worth recording, because it makes one
of the five collapsed cases unreachable in practice: a *governed* collection
normalizes every stored record inside `planning.query`, so a directly-edited
malformed `motivation` entry refuses the whole collection before the review sees
a row. The review reports that through the existing bounded
`collections_unavailable` counter, with no path, title, or reason — the same
non-answer the per-reference outcome would have given. The per-reference
handling stays, because it is the behaviour if that validation ever loosens, and
it is covered directly at the unit level.

## Bounding

A second budget, deliberately not a share of `execution_budget`, so
`budget_exhausted` keeps meaning exactly one thing: a Records view was skipped.
Distinct references are collected across the **retained** items — after
ordering and after truncation — or the budget would be spent on items that never
appear in the response. One batch call resolves them all, and each distinct
reference is resolved at most once per call.

## Non-goals

**The successor is not named.** Naming it needs a second hop through a wikilink
to a distinct disclosure subject, and the obvious helper `evolution._load_link`
resolves through the cache with no release check at all. Emitting nothing about
the successor keeps the entry shape uniform and stops "successor known" from
becoming a second probe channel.

**Verdict-based flagging is deferred to a later change.** No verdict history
exists anywhere in the source: `observe_memory` carries the current verdict
forward or clears it and never records what it replaced, so "the verdict
changed" has nothing to diff against. Separately, `semantic_units.py` states
that a refuted claim keeps active standing, so treating `refuted` as
plan-invalidating would be a new epistemic stance rather than a mechanical
detail. It also wants fragment-resolved reference targets, so a reference
addresses a unit rather than fanning across a whole page. Built today it would
be page-wide guesswork.

**This is pull, not push.** Plan-progress is a standalone read-only mode, not an
attention category: every `AuditFinding` anchors on a page path and a Planning
item is not a page, and `audit()`'s corpus excludes Planning collections.
Shipped tests pin that the review yields no triageable reference and is not an
attention category, and both stay true.

## Testing

Red-first, and the disclosure suite written **last** so it is adversarial
against the finished implementation rather than co-designed with it. It takes
two forms:

- *Structural*: four plans citing an absent reference, a duplicated identity, a
  governance-blocked page, and an access-tier-excluded page produce identical
  entries once each item's own authored reference is set aside, one identical
  divergence block, and one tally.
- *Equality, and stronger*: two vaults differing only by whether an unreleased
  page exists produce equal responses once `generated_at` is dropped. That is
  the faithful statement of "cannot be used to probe for hidden knowledge", and
  it catches count drift, shape drift, and tally movement the example form
  cannot. Each equality assertion carries a control that flips only the release
  decision, so it cannot pass by the page being unreachable.
