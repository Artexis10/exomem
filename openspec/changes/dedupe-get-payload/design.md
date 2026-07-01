# Design - dedupe get payload

## Context

`get_page()` in `src/kb_mcp/get_page.py` reads a vault-relative path, parses it via
`find_module._parse_page` for `frontmatter`/`body`, reads the raw file text once more
into `content`, and hashes that raw text into `content_hash`
(`src/kb_mcp/vault.py::content_hash`, sha256 over the full raw bytes so a
frontmatter-only concurrent edit still trips the drift guard). `GetResult.as_dict()`
serializes all of `path`, `frontmatter`, `body`, `content`, `content_hash`, and `mtime`
unconditionally. `op_get` in `commands.py` (~lines 847-928) calls this for the default
(non-`frontmatter_only`) branch and returns the dict as-is, plus optional
`history`/`links`.

Because `content` is the raw file text (frontmatter delimiters + `body`), and `body` is
already returned separately, every default `get` call ships the page body twice and the
frontmatter block twice (once raw inside `content`, once parsed as `frontmatter`). For
large pages this is a real, unnecessary per-call token cost.

The only consumer of `content_hash` is the two-writer drift guard: a caller reads a
page, then later calls `edit(...ingredient, expected_hash=<that hash>)`; `edit` (via
`vault.content_hash`) recomputes the hash of the file currently on disk and refuses the
write (`STALE_EDIT`) if it no longer matches. This flow only needs the hash string, not
the raw bytes it was computed from — `content_hash` is already computed server-side
inside `get_page()` independent of what `as_dict()` chooses to expose.

`get` is one entry in the unified command registry (`commands.py` `_SPEC`,
`("get", op_get, 1, False, False, "path", _MCRC)`) with surfaces `{mcp, rest, cli}`; MCP
schema, REST route, CLI subcommand, and OpenAPI path are all generated from `op_get`'s
signature and docstring (per the `command-surface` capability). No separate per-surface
schema exists to drift.

## Goals / Non-Goals

**Goals:**

- Stop shipping the page body (and effectively the frontmatter) twice in the default
  `get` response.
- Keep `content_hash` computed over the raw file text exactly as today, so
  `edit(expected_hash=...)` round-trips need no caller-side changes.
- Provide an explicit, discoverable opt-in (`include_raw`) for callers that genuinely
  need the raw file bytes (e.g. byte-for-byte tooling, external diffing).
- Keep the change isolated to `get`'s payload shape — no ranking, retrieval, or
  registry-mechanism changes.

**Non-Goals:**

- No change to `frontmatter_only=true` behavior (already excludes body/content).
- No change to `body` semantics or to what `edit(new_body=...)` / the skill workflow
  consumes.
- No change to how `content_hash` is computed, or to `edit`'s `expected_hash` handling.
- No change to `links`/`history` composition.
- No deprecation period, feature flag, or dual-shape transition — single-user surface;
  ship the break as a clean, clearly-flagged cut.
- No change to the `command-surface` registry mechanism itself (single registry,
  schema-fidelity snapshot, REST/CLI generation) — `include_raw` is just a new
  parameter flowing through the existing generation path.

## Decisions

### Keep `body`, drop `content` by default (not the reverse)

`body` is what `edit(new_body=...)` round-trips and what the skill workflow already
consumes when drafting an edit or citing a page. `frontmatter` is already returned
structured (a dict), so the raw `content`'s only unique value is exact byte fidelity
(e.g. YAML formatting quirks, exact whitespace) — a need real but rare enough to make
opt-in, not default.

Alternative considered: drop `body` and keep `content`, forcing callers to split
frontmatter out of the raw text themselves. Rejected — that regresses every existing
consumer of `body` and duplicates parsing logic that already lives server-side in
`find_module._parse_page`.

### `include_raw: bool = false` as the opt-in escape hatch, not a deprecation period

Add `include_raw: bool = false` as a new parameter on `op_get` / `get_page` (or
`GetResult.as_dict`). When `true`, the returned dict gains `content` with the exact raw
file text. This is a single-user, personal-installation MCP/REST/CLI surface — a
clean, prominently-flagged breaking change is simpler and safer than maintaining two
response shapes (e.g. a `content: null` default vs. omission, or an old/new schema
selected by a version flag) across a transition window that nobody but the maintainer
would observe anyway.

Alternative considered: keep `content` present but empty-string by default, only
populating it under `include_raw`. Rejected — an always-present-but-usually-empty key
still confuses callers about the contract and does not save the schema-description
token cost; omitting the key entirely is the existing convention this registry already
uses for optional fields (`history` and `links` are absent unless requested).

### `content_hash` computation is untouched

`get_page()` still reads the raw file text and computes `content_hash` over it via
`vault.content_hash()` unconditionally, regardless of `include_raw`. Only whether the
raw text is included in `as_dict()`'s output changes. This means
`edit(expected_hash=get(path=...).content_hash)` keeps working exactly as before with
no caller-side changes, and the drift guard's frontmatter-plus-body coverage
(`tests/test_drift_guard.py::test_hash_covers_frontmatter_change`) is unaffected.

### This change is isolated from `improve-find-latency-token-cost`

Per review, `improve-find-latency-token-cost` is explicitly scoped as
results-identical latency/observability work for `find` (its own design states "no
change to default `find` return shape"). `get`'s payload change is a protocol break on
a different command. Riding it along with a results-identical change would blur the
review signal of both changes, so this is its own OpenSpec change, reviewed and
released independently.

## Risks / Trade-offs

- An existing caller relies on the default `content` field and silently breaks ->
  flagged prominently in the proposal, the CHANGELOG breaking-change footer, and this
  design doc; single-user surface makes this reviewable before it ships, and
  `include_raw=true` is a one-parameter fix for any caller that needs it.
- MCP schema fixture drifts silently -> `tests/fixtures/mcp_tool_schemas.json` is
  regenerated and reviewed as part of this change; `tests/test_mcp_schema_fidelity.py`
  fails loudly otherwise.
- Docs/scaffold text quietly promises raw `content` that no longer ships by default ->
  swept `src/kb_mcp/_scaffold/_Schema/**` and `README.md`; no reference to `get`
  returning raw `content` by default was found (see proposal Impact section), so no
  edits are required, but the sweep is recorded here so a future change touching `get`
  docs can trust this baseline.
- `docs/capabilities.md`'s `get` row goes stale ("Returns frontmatter + body + raw
  content.") -> regenerate via `scripts/generate-capabilities.py` alongside the
  docstring update, since that table is generated straight from `op_get`'s docstring.

## Migration Plan

No data migration — this is a response-shape change only, no stored vault format
changes. Deploy as a single code change. Callers that need the raw file text pass
`include_raw=true`. There is no feature flag or transition window; the change ships as
a flagged breaking change in one release, consistent with the single-user nature of
this surface.

## Open Questions

None for implementation.
