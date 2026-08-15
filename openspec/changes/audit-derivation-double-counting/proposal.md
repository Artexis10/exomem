# Proposal: audit-derivation-double-counting

## Why

Two gaps in the existing audit surface are currently invisible to every check:

- **Double-counted evidence.** Two notes derived from one source, then a third
  citing both as independent support, passes silently — the classic
  circular-support failure where one blog post is laundered into "multiple
  sources agree", in a system whose pitch is provenance.
- **Circular evidence.** Nothing walks support chains for cycles (A supports B
  supports A via a derived note).

Both are visible only by walking the `derived_from` graph that `sources:`
frontmatter already implies. No existing category does that traversal.

## What Changes

- Add a new, opt-in, read-only audit category, `derivation_double_counting`,
  that walks `sources:` (`derived_from`) chains and reports two finding kinds:
  - `support_collapse` (severity `info`): an active, read-write, compiled page
    (`research-note` / `insight` / `failure` / `pattern` — the types whose
    `sources:` carries substantive provenance) cites two or more sources whose
    ancestor chains converge on a shared node, so citing them as independent
    support double-counts that ancestor. The finding names the nearest such
    shared ancestor — one finding per converging situation, not one per node
    in a multi-hop shared tail — and never names the citing page itself.
  - `cycle` (severity `warn`): a `sources:` chain that is reachable from
    itself, including a direct self-reference.
- Bound the chain walk explicitly by depth (`EXOMEM_DERIVATION_MAX_DEPTH`,
  default 12) and by a shared total-edge budget across the whole audit pass
  (`EXOMEM_DERIVATION_MAX_EDGES`, default 50,000 — measured and validated
  against a synthetic ~5,000-file vault; see design.md D2). Whenever either
  cap stops exploration before it completes, emit a dedicated `truncated`
  finding (`info`) naming which cap(s) were actually hit, so a capped run
  reads as "incomplete", never as a false "nothing found".
- Observe only: the category never mutates a note, never rewrites a relation,
  never downgrades or demotes anything, and never blocks a write. It is
  absent from `ALL_CATEGORIES` (the default audit sweep) — callers opt in via
  `audit(categories=["derivation_double_counting"])`.

Explicitly out of scope: flagging conclusions whose only roots are agent
sessions. The approved design allows this only if it falls out of the
traversal cheaply; no existing page-type/tag taxonomy identifies an "agent
session" page in this codebase, so adding one would be inventing new
vocabulary rather than reusing the traversal — deferred to a follow-up change
if that taxonomy is ever introduced. Wiring this category into the `attention`
composed queue (`attention.py`'s `ATTENTION_CATEGORIES`) is also out of
scope, matching the existing `missing_sources` category, which is likewise
optional and reachable only via `audit()` directly.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `attention-queue`: adds an optional, non-default, informational/warn-only
  `derivation_double_counting` audit category surfacing derivation-chain
  support-collapse and cycle findings, requested via
  `audit(categories=["derivation_double_counting"])`.

## Impact

- `src/exomem/audit.py` only: one new optional category, its check function,
  and the bounded-walk helpers it uses. No new module, dependency, model,
  background process, or index.
- Pure-substrate: the traversal is deterministic graph reachability over
  frontmatter already on disk. No score, no confidence float, no ranking
  change to `find` or `attention`.
- Existing pages are untouched; this is a new, cheap-to-ignore review queue,
  not a migration.
