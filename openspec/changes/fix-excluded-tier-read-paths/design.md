# Design: fix-excluded-tier-read-paths

## Context

`access.access_tier(vault_root, rel_path)` (`access.py:161`) already resolves any
vault-relative path to one of `excluded` / `readonly` / `append-only` /
`read-write`, live-reloaded on content hash (`access.py:78`). `is_indexable`
(`:181`) is `True` for everything except `excluded`. Index-time lanes call it
(`find_corpus.py:240`, `embeddings.py:1356`, `claims.py:532`); the direct-read
surfaces do not. `review_context.py` is the in-repo reference for enforcing the
tier on a read path. This change is pure enforcement wiring — no new policy.

## Goals / Non-Goals

Goals: every read surface that can return or enumerate a page consults the same
tier resolver the index lanes already trust; excluded refusal is
indistinguishable from absence; the graph lane treats excluded pages as
non-existent for seeding, expansion, and edges.

Non-Goals: representation ceilings, redaction, audience/purpose, any
`_Governance/` file (all deferred to the governance changes). No change to what
`excluded` *means* — only where it is honored. No change to `readonly` /
`append-only` semantics.

## Decisions

### D1 — One helper, applied at each surface's resolve point
Add a small `access.refuse_if_excluded(vault_root, rel_path)` returning a
uniform sentinel/raise, and call it immediately after each surface normalizes a
path to vault-relative form: `get_page` after path validation
(`get_page.py:103-117`), `query_data` and `video_frames` after
`resolve_under_vault`. This keeps the tier check in one place and mirrors
`review_context`'s existing usage.

### D2 — Refusal is byte-identical to NOT_FOUND
An excluded `get_page`/`read_media`/`query_dataset` returns the exact error code,
shape, and message a missing path returns, and never interpolates the probed
path. Rationale: a distinct "forbidden" error, or a path echo, tells a caller the
item exists — the same existence-oracle class the governance release plane later
guards against. Absence and denial must be indistinguishable at the boundary.

### D3 — overview prunes, not annotates
`overview` removes excluded subtrees from the `os.walk` (dir-prune) and skips
excluded files, so they contribute to neither the tree nor any count. It does not
emit a "hidden: N" marker — that count would itself leak subtree size.

### D4 — Graph lane filters at three points
`graph_context` applies `is_indexable` to (a) seed resolution, (b) node
materialization, and (c) edge endpoints — an excluded page is never a seed,
never surfaces as a neighbour, and never appears as either end of a returned
edge. This matches the `find` hit-assembly filter (`find.py:2601`) that the graph
lane currently bypasses.

## Risks / Trade-offs

- **Existing fixtures with excluded trees shift**: `overview` counts and any test
  asserting on excluded content change. Resolution: audit `tests/test_overview.py`
  goldens within this change rather than discovering the drift in CI.
- **Performance**: one extra `access_tier` call per read; the config is cached on
  content hash, so cost is a dict lookup. Graph filtering adds an `is_indexable`
  check per candidate node (≤ the node cap, ~40) — negligible.
- **Over-refusal**: a caller legitimately reading an excluded path they own now
  gets NOT_FOUND. This is the intended contract ("truly private"); such content is
  reachable only by removing it from `excluded` in `_access.yaml`.

## Migration Plan

Additive enforcement; no data migration. Behavior change is limited to excluded
paths, which by definition were meant to be invisible. No config format change.

## Open Questions

None blocking. Follow-up: whether `list_inbound_links` / `provenance_report`
need the same gate (tracked under the release-gate change's coverage sweep).
