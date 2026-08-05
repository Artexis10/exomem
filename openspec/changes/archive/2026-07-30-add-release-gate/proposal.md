# Proposal: add-release-gate

## Why

`add-governance-kernel` can decide a disclosure ceiling but nothing enforces it.
This change is the **release plane**: it makes every content-returning surface
render only what each item's decision permits, and it introduces the disclosure
ladder that turns a single "ceiling" into a concrete representation on the wire.

The separation of planes is the whole product thesis: retrieval stays broad and
creative over the full indexed corpus (the relevance plane is unchanged), but a
deterministic gate sits between ranked candidates and the response, so *internal
discoverability never implies external releasability*. Titles, paths, counts,
snippets, graph neighbours, and ranking signals are all "release" — not just
bodies — so the gate operates on structured candidates before serialization, not
on finished text.

Two verified code realities shape the design. First, the one dispatcher shared by
MCP, REST, hosted, and CLI is `writer_lease.invoke_command` — **not**
`command_surface.bind_vault` (which is MCP-only); the `EXOMEM_RETRIEVE_INJECT`
hook deliberately travels the REST/CLI paths that bypass `bind_vault`, so the
terminal scrubber and post-filter must sit at `invoke_command`. Second, the find
hot-cache stores deep copies of `Hit` objects shared across principals, so
decisions must be computed in `op_find` *after* `find()` returns — never inside
`find()`, or one principal's decisions would be cached for another.

This change also ships the **default-on credential egress block** (confirmed with
the owner): a deterministic secret scrubber that blocks credential-shaped strings
at egress even on an ungoverned vault, with one standing rule to disable it. It is
the single intentional behavior change for vaults with no `_Governance/`.

## What Changes

- A **disclosure ladder** L0 `none` → L1 `notice` → L2 `constraint` → L3
  `abstract` → L4 `excerpt_redacted` → L5 `excerpt` → L6 `full`. A rule sets a
  ceiling; the assembler renders the highest permitted, lowest sufficient
  representation.
- A **per-level projector** `project(payload, level)` that is the *only* path from
  a Hit / page / pack element / unit to a wire dict. Raw serializers
  (`Hit.as_dict`/`as_compact_dict`, etc.) are removed from the egress path.
  Ranking signals, `graph.seed`, `relation_match`, `matched_units`,
  `superseded_by`, `parent_ref` are content oracles — emitted only at L5–L6, and
  stripped at every level when they name a sub-notice path. A command with no
  registered projector **fails closed** (cannot emit path/title/excerpt).
- **Decision annotation in `op_find`** strictly after `find()` returns
  (`commands.py:901→902`): withheld items dropped and replaced by notices;
  count backfilled from a pre-committed over-fetch pool so the shown count is
  request-deterministic; graph-expanded hits whose only provenance is a withheld
  seed dropped. The shared `_FIND_CACHE` stays principal-free; a separate memo
  keys decisions per `(fingerprint, path, audience, purpose, grants-hash)`.
- **Canonical audience** resolved at each surface boundary (MCP/REST/hosted/CLI)
  into one comparable id space; stdio/CLI = `owner`; unresolved-but-expected
  fails closed to most-restrictive. Threaded to every read leaf. `purpose` added
  as an optional param on the recall leaves.
- **Withhold-tokens**: single-use HMAC capability tokens bound to
  `(audience, item content-fingerprints, max level, TTL)`, minted into `withheld`
  notices, consumed once in a per-machine sidecar store — a second instantiation
  of the hosted transfer-grant-v2 + JTI pattern.
- **Terminal secret scrubber + withheld cross-check** in
  `writer_lease.invoke_command`, covering MCP/REST/hosted/CLI, with an MCP-layer
  second pass in `bind_vault`. Always-on (credential block); structural-field
  allowlist prevents false positives on `content_hash`/`ref` fields.
- Enforcement extended to `op_get`, the graph lane, deep packs, media/dataset,
  and transfer-download issuance. `read_media` frames and download grants mint
  only after a release decision.

## Capabilities

### New Capabilities

- `release-gate`: a deterministic per-item release plane — disclosure ladder,
  single per-level projector, decision annotation with request-deterministic
  backfill, canonical audience resolution, single-use withhold-tokens, and an
  always-on terminal secret scrubber at the shared dispatcher — enforced across
  every content-returning surface with an empty-policy fast path.

### Modified Capabilities

- `find-recall-efficiency`: the hot cache stays principal-free; decisions are a
  separate per-request memo, never a cache-key fragment.
- `context-packs`: pack elements carry decisions and never contain sub-notice
  content.
- `graph-find-ranking`: graph expansion never seeds from a sub-notice item and
  strips provenance naming withheld paths.
- `get-payload-shape`: `get`/`read_memory` render at the item's decision level.

## Impact

- Code: new `src/exomem/governance/{principal,egress,scrubber,tokens}.py`;
  edits to `commands.py` (annotate at `op_find` `:901→902`; `op_get`,
  `op_graph_context`, `op_get_video_frames`, `op_transfer_artifact` gating;
  `purpose` params), `find_types.py` (decision-aware projection + `Hit.decision`),
  `find.py` (backfill pool, graph-seed guard, decision memo), `context_pack.py`,
  `command_surface.py` (audience contextvar beside `mcp_retry_scope`; MCP
  second-pass filter), `writer_lease.py` (terminal postfilter + scrubber call),
  `server_rest.py`, `server_hosted.py`, `__main__.py` (per-surface audience
  set-points), `tool_surface_contract.json` (regenerated for the `purpose` param).
- Tests: `tests/test_governance_egress.py`, `test_governance_principal.py`,
  `test_governance_tokens.py`, `test_governance_postfilter.py`, plus surface
  parity and overhead micro-gate; existing goldens + latency gate stay green.
- Explicitly NOT in scope: the `govern_memory` authoring tool (next change),
  receipts, bridges, redaction span maps (L4 detail), budgets.
