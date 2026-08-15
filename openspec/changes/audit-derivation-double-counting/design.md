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
that branch stops expanding. `EXOMEM_DERIVATION_MAX_EDGES` (default **50,000**,
revised from an initial 2,000 — see below) is a budget *shared across every
walk in one audit pass* — it bounds total work regardless of how many origin
pages need closures computed, protecting against a vault where many pages
each have deep, overlapping chains.

`walk_cache` memoizes only by exact **top-level start key** — it does not
splice one node's already-resolved closure into another node's in-progress
BFS. That reuse was prototyped and rejected: a node's own bounded closure is
computed relative to *its own* `max_depth` hops from itself, so splicing it
into a caller with fewer hops of budget remaining silently lets that caller's
result reach further than its own cap nominally allows, *and* under-reports
`truncated` for exactly the walk that should have set it (traced by hand
against a 6-node chain with a lowered depth cap before it was ever written
into the tree). Every walk therefore re-examines its own reachable edges
independently; the edge budget in this design is real total work, not an
artifact of missing memoization, which is why it needed re-deriving rather
than patching over.

Each `_DerivationWalk` records *why* it stopped short, not just whether it
did: `truncated_reasons` is a subset of `{"depth", "edges"}` (empty means
complete). Hitting either cap contributes its reason; the check emits exactly
one aggregate `truncated` finding (`info` severity) per audit run, naming
every reason actually observed and both configured limits — never a bare
`depth_capped` boolean that would misreport an edge-budget truncation as a
depth-capped one (mutation-testing review, correction round 1).

**Deriving the edge-budget default.** The initial 2,000 default was picked
without measurement and was wrong: mutation-testing review measured this walk
(no cross-walk closure reuse, per the rejected alternative above) consuming
roughly 10 edges per sourced page in a chain-shaped test graph, exhausting a
2,000 budget at ~200 sourced pages — nowhere near the vault this ships into
(~2,900 markdown files at review time). Re-derivation, then validated
empirically rather than only extrapolated:
- Assume a generously-sized target of **~5,000 markdown files**, up to half
  (2,500) carrying `sources:` — deliberately over-estimating density, since a
  real vault's `sources:` fraction is typically much smaller.
- At the reviewer's measured ~10 edges/sourced-page: 2,500 × 10 = 25,000.
  Doubled for margin → **50,000**.
- Validated with a synthetic 5,000-file vault (2,500 sourced notes across two
  citation tiers — notes citing raw `Sources/` leaves directly, and notes
  citing two of those first-tier notes, matching this codebase's actual
  `sources:` convention rather than an unrealistic multi-thousand-hop
  braided chain) at the *old* default: it truncated by `edges` alone at 2,000.
  At **50,000**: it completed with zero truncation in ~1.3s wall-clock. The
  old default was also independently confirmed insufficient on a
  deliberately adversarial deep/braided chain (2,500-hop citation history);
  the new default terminates that scenario promptly too, correctly reporting
  `truncated` with both `depth` and `edges` reasons — depth capping a chain
  that genuinely runs thousands of hops deep is the depth cap doing its
  documented job, not an edge-budget sizing defect.
- The env override (`EXOMEM_DERIVATION_MAX_EDGES`) remains for a vault denser
  than this baseline.

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
finding, with the *complete* set of exclusions `relation_debt`/
`missing_sources` already apply: `index.md`/`log.md`, inactive status, and —
added in the correction round, where its absence was flagged as a genuine gap
rather than a comment inaccuracy — the hub/snapshot tag and
`-architecture`/`-snapshot`/`-catalog-snapshot` slug-suffix skip. A hub or
snapshot page is *expected* to fan its `sources:` out from a shared root by
convention, and would otherwise dominate this queue with non-actionable
noise, exactly the failure mode those exclusions already exist to prevent
elsewhere. Cycle detection is NOT type-gated — a cycle is a structural defect
in the graph itself, findable from any node with outgoing `sources:` edges,
regardless of that node's page type or status. This asymmetry is deliberate:
collapse is a soft heuristic about a specific page's argument, cycle is a
hard graph-shape problem.

### D5 — Severity split, never `error`

`cycle` is `warn` (a `sources:` chain that supports itself is a real logical
inconsistency in the provenance graph). `support_collapse` and `truncated`
are `info` (review candidates requiring human judgment about whether
citations are genuinely independent, and a capped-run notice, respectively).
Neither is ever `error`: this check observes, it does not gate writes or
imply a defect the way `frontmatter_compliance` or `duplicated_sidecar`'s
`recovery_only` case do.

### D6 — A citing page is never its own shared ancestor; one tail collapses to its nearest node

`collapse_roots` excludes the citing page's own key outright: since that key
is one of the page's own direct sources by construction, any structure where
it also appears in another direct source's closure is necessarily a cycle
back through the citing page itself (unavoidable — reaching a page from one
of its own citations requires a path back to it) and is already reported
separately as a `cycle` finding. Reporting it *again* as the page's own
"shared ancestor" is nonsensical and was a correction-round finding
(mutation-testing review, blocking 3).

Separately, a single converging tail (`A` and `B` both citing `C`, `C` citing
`D`, `D` citing `E`) previously emitted one finding per node in the shared
tail (`C`, `D`, *and* `E`) — three findings for one situation, contradicting
the spec's own "one finding … naming `S`" scenario (major 4). `_nearest_
shared_roots` drops a candidate root `Y` whenever some *other* candidate `X`
can reach `Y` (`Y` is further upstream, discovered on the same tail),
independent of iteration order — it checks every pair rather than depending
on which candidate is visited first, which matters because a naive
sorted-then-first-wins approach silently keeps the wrong (furthest, not
nearest) node under adversarial key naming. If every candidate ends up
mutually dominated (a cycle among the candidates themselves), one
deterministic representative survives rather than the finding being silently
dropped.

### D7 — Unresolved targets get a reconstructed path, not an internal key

`display_path` prefers the resolved page's real `rel_path`; when a `sources:`
target does not resolve to a known page, it now reconstructs a plausible
vault-relative path (`kb_prefix() + raw + ".md"`) from the original raw
wikilink text tracked alongside the canon key, rather than falling back to
the internal lowercase, extension-stripped canon key. Every other path
`AuditFinding` reports — `path` and every path embedded in `meta` — is a
vault-relative rel_path an agent can open directly; the canon key broke that
contract silently for exactly the pages a reviewer most needs to inspect
(dangling/unresolved provenance).

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
