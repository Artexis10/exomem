# Proposal: fix-excluded-tier-read-paths

## Why

`_access.yaml`'s `excluded` tier is documented as "truly private — invisible to
find/embedding AND unwritable" (`access.py:1-9`, `39`). That contract holds only
at index time. Direct-read surfaces never consult `access.is_indexable`, so an
`excluded` page is still fully readable and enumerable by any caller who knows or
guesses its path:

- `get_page.get_page` (`get_page.py:68`) has no tier check — only the reserved
  hosted-vault-name guard (`:81`).
- `overview.overview` (`overview.py:106`) walks the tree with `os.walk` and zero
  `access` imports — excluded subtrees are listed and counted.
- `query_data.query_data` and `video_frames.get_frames` (`:95`) resolve paths
  under the vault with no tier gate.
- The graph lane `epistemic_graph.graph_context` (`:825`) seeds and materializes
  nodes without `is_indexable`, so an excluded page surfaces as a neighbour.

`review_context.py` already enforces the tier on its read paths (`:73`, `:243`,
`:332`, `:396`) — this change brings the remaining direct-read surfaces up to
that same standard. It is a correctness/privacy fix in its own right and a
prerequisite for the governance release plane, which layers representation-level
ceilings on top of a trustworthy `excluded` floor.

## What Changes

- `get_page`, `overview`, `query_dataset`/`query_data`, `read_media`/`video_frames`,
  and the graph-context lane consult `access.access_tier` / `access.is_indexable`
  and refuse or omit `excluded` paths.
- Refusal is **indistinguishable from absence**: an excluded direct read returns
  the same shape and text as a genuine `NOT_FOUND`, and never echoes the probed
  path — otherwise the error itself is an existence oracle.
- `overview` prunes excluded subtrees from the walk and from all counts.
- The graph lane filters excluded pages from seeds, nodes, and edges (never a
  seed, never a neighbour, never an edge endpoint).

## Capabilities

### Modified Capabilities

- `get-payload-shape`: `get`/`read_memory` refuse `excluded` paths
  indistinguishably from missing.
- `vault-overview`: `overview` hides excluded subtrees from structure and counts.
- `graph-find-ranking`: the graph-context lane never seeds from or returns
  excluded pages.

(`query_dataset` and `read_media` are Tier-2 command-surface ops; their
enforcement is registry-level and asserted at the command layer.)

## Impact

- Code: `src/exomem/get_page.py`, `src/exomem/overview.py`,
  `src/exomem/query_data.py`, `src/exomem/video_frames.py`,
  `src/exomem/epistemic_graph.py` (graph_context seed/node/edge filtering).
- Tests: new `tests/test_access_read_paths.py` (per-surface refusal + absence
  indistinguishability + graph seed/neighbour exclusion + command-layer sweep);
  `tests/test_overview.py` fixtures audited for excluded-tree count shifts.
- No new dependencies; no schema changes. Existing `test_access.py` remains the
  index-time reference.
- Explicitly NOT in scope: representation-level ceilings, redaction, any
  `_Governance/` machinery (those land in `add-governance-kernel` /
  `add-release-gate`).
