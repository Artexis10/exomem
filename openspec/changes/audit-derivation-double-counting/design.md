# Design: audit-derivation-double-counting

## Context

`sources:` frontmatter is the one place provenance is recorded, and the
existing graph builder (`epistemic_graph.py`) already maps it to a
`derived_from` edge (page → each cited source) when it materializes the graph
sidecar. No audit check, however, walks that edge transitively. Two failure
modes are therefore invisible:

- A page cites two sources that both trace back to one shared ancestor — the
  ancestor's support gets counted twice under the appearance of independent
  corroboration.
- A `sources:` chain loops back on itself.

This change adds one new, opt-in audit category that performs that walk
directly over already-parsed `ParsedPage` frontmatter — the same data source
`missing_sources` and `relation_debt` already use — rather than depending on
the `epistemic_graph` SQLite sidecar being present or fresh. That keeps the
check self-contained and consistent with every other audit category, which
reads vault truth directly rather than a derived index.

## Goals / Non-Goals

**Goals:** detect support-collapse (a "diamond" of derivation converging on a
shared ancestor) and circular derivation over the `sources:` graph; bound the
walk explicitly (depth + total-edge budget) so a dense, thousand-note vault
cannot turn one audit call into an unbounded traversal; make a hit cap visible
in the findings rather than silently under-reporting; stay strictly
observe-only (never mutate, never auto-act); avoid false positives on
genuinely independent sources.

**Non-Goals:** ranking or scoring how "bad" a collapse is; auto-fixing or
auto-superseding anything; wiring into the `attention` composed queue (out of
scope — matches the existing `missing_sources` precedent); flagging
agent-session-rooted conclusions (no existing taxonomy identifies those pages;
deferred, see proposal.md); expanding the `derived_from` edge set to also
include explicit typed `## Relations` blocks — the approved design names only
`derived_from`/`sources:`, which in this codebase is exactly the `sources:`
frontmatter field.

## Decisions

### D1 — One bounded BFS serves both cycle detection and ancestor closure

A single helper, `_bounded_ancestor_walk(direct_sources, start, max_depth,
budget)`, does one BFS from `start` over `derived_from` edges. It returns the
set of reachable ancestors, whether `start` is reachable from itself (a
cycle, with one reconstructed witness path), and whether the walk was
truncated by the depth or edge cap. A `seen` set makes the walk terminate
regardless of cycles in the underlying graph — the cycle check therefore
never risks infinite recursion, and needs no separate unbounded pass.

Cycle detection asks, for every node with outgoing `sources:` edges: "is
`start` in its own bounded closure?" Support collapse asks, for a page citing
two or more sources directly: "do any two of those sources' bounded closures
(including each source itself) intersect?" A shared element in that
intersection is the double-counted ancestor. This unifies both failure modes
onto the same primitive instead of two separate traversal implementations.

### D2 — Two explicit, env-overridable caps, not one

`EXOMEM_DERIVATION_MAX_DEPTH` (default 12) bounds hops from any node before
that branch stops expanding. `EXOMEM_DERIVATION_MAX_EDGES` (default 2000) is
a budget *shared across every walk in one audit pass* — it bounds total
work regardless of how many origin pages need closures computed, protecting
against a vault where many pages each have deep, overlapping chains. Per-key
walk results are memoized (`walk_cache`) so a shared ancestor visited from
multiple origins is only walked once. Hitting either cap sets a `truncated`
flag; the check emits exactly one `truncated` finding (`info` severity) per
audit run if any walk was capped, naming both limits, rather than emitting
one per page (which would be noisy) or staying silent (which would read as
"no problem found").

### D3 — Self-references are graph edges, not filtered inputs

`_derivation_direct_sources` keeps a page that cites itself in `sources:`
rather than dropping it at graph-construction time. This is what lets the
degenerate single-node cycle (`A` cites `A`) surface through the same cycle
detector as any longer cycle, satisfying "cycle detection must terminate on
self-referential chains" as a directly observable case rather than a special
code path.

### D4 — Origination gate mirrors `missing_sources`

Only active, read-write pages of the four provenance-bearing compiled types
(`research-note`, `insight`, `failure`, `pattern` — `_SOURCES_REQUIRED_TYPES`,
already defined for `missing_sources`) originate a `support_collapse`
finding, with the same `index.md`/`log.md` and inactive-status exclusions
`relation_debt`/`missing_sources` already apply. Cycle detection is NOT
type-gated — a cycle is a structural defect in the graph itself, findable
from any node with outgoing `sources:` edges, regardless of that node's page
type or status. This asymmetry is deliberate: collapse is a soft heuristic
about a specific page's argument, cycle is a hard graph-shape problem.

### D5 — Severity split, never `error`

`cycle` is `warn` (a `sources:` chain that supports itself is a real logical
inconsistency in the provenance graph). `support_collapse` and `truncated`
are `info` (review candidates requiring human judgment about whether
citations are genuinely independent, and a capped-run notice, respectively).
Neither is ever `error`: this check observes, it does not gate writes or
imply a defect the way `frontmatter_compliance` or `duplicated_sidecar`'s
`recovery_only` case do.

## Risks / Trade-offs

- **False positives on legitimately-independent-but-topically-related
  sources.** The check only looks at graph structure (does an ancestor chain
  literally converge), never content — it cannot tell "these two sources
  restate one interview" from "these two sources happen to cite the same
  well-known reference work". This is explicitly a review candidate
  (`info`), not an auto-judgment, and the false-positive guard (independent
  chains produce no finding) is covered directly in tests.
- **A depth/edge cap can hide a real collapse or cycle beyond the horizon.**
  Mitigated by making truncation visible (D2) rather than silent; a reviewer
  seeing the `truncated` finding knows to widen the caps or investigate the
  densest chains directly rather than trusting a false "clean" result.
- **Memoized walk results are pass-scoped only.** Caches (`walk_cache`,
  the shared edge budget) live for the duration of one `_check_derivation_
  double_counting` call and are rebuilt on every audit invocation — no stale
  cross-run state, at the cost of repeating work across separate audit calls.
  Given the category is opt-in and not part of the default sweep, this
  trade-off favors simplicity over a persistent cache.

## Migration Plan

Additive only: one new optional audit category, absent from
`ALL_CATEGORIES` and from the `attention` composed queue's category set.
Existing callers and existing findings are unaffected.

## Open Questions

None.
