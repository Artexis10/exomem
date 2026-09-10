# Design — repair the Records writer

## Context

See proposal.md for motivation. Facts in the tree that fix the shape (main at `22e7bfc8`, origin/main at `b7af6bc0`, no Records changes between):

- `records._validate_values(manifest, item)` owns item validation for both profiles and has the manifest, therefore the storage strategy, in scope. It calls `_validate_representable(value)`, which refuses any string containing `\n`/`\r` (or over the byte cap) with no field path, and raises `SCHEMA_UNKNOWN_FIELD` without naming the fields. `manifest.schema.validate` raises on the first type or enum failure.
- `vault.serialize_frontmatter` → `_format_yaml_line` → `yaml_scalar` renders strings needing quotes via `json.dumps`, which is YAML double-quoted style; `yaml.safe_load` reads `"a\n\nb"` back as the original string. Markdown-item frontmatter can therefore already carry line breaks losslessly; only the gate forbids it. Markdown-log headings, notes and delimited child rows genuinely cannot (`record_formats.py` ~L740-810).
- Item audit markers are collected from the adapter snapshot's records, i.e. files under `storage.source` (`records._audit_markers`). `MarkdownItemsAdapter.read` walks `storage.source` only. `recall_policy` rejects every Records-layer descendant except an exact `_collection.md`. A directory beside `Items/` is therefore outside items, audit and recall by construction.
- `record_memory` has a closed action set and a per-action argument matrix; changing either moves the tool-surface fingerprint pin. Bootstrap text is not touched by this change, so the compact byte ceiling is not at risk.
- The shipped convention since #948: refusals name the argument at fault in `details`.

## Goals / Non-Goals

**Goals:** make Markdown-item ledgers with multi-line values writable without escaping; make every candidate refusal actionable in one round trip; never lose a refused observation; make a blocked ledger countable through inspection and inventory.

**Non-Goals:** presentation rendering changes; schema-evolution advice derived from held items; due-state carriers or attention families for held counts; `plan_memory` hold arguments (the mechanics land in the shared substrate so Planning can adopt them later without redesign); any change to how items are named or stored; repairing live vault content from code.

## Decisions

### D1 — Strategy-aware representability with a round-trip proof

`_validate_values` passes the storage strategy and a field-path accumulator into the representability check. Markdown-log keeps the existing rule (no line breaks, no `\r`, byte cap) because its rendering cannot carry them. Markdown-item values drop the line-break rule and keep the byte cap; instead, after schema normalisation, the writer serialises the complete candidate frontmatter (system fields plus values) with `vault.serialize_frontmatter`, parses it back with `vault.parse_frontmatter(text, strict=True)` — the reader `MarkdownItemsAdapter.read` uses (`record_formats.py` ~L497); note `render_markdown_item` emits the audit marker as a `#` YAML comment inside the block, which that parser drops — normalises through the schema, and compares field by field. Any difference refuses `UNREPRESENTABLE_RECORD_VALUE` naming the field; nothing is staged. Dataset strategies are query-only in this delivery, so the rule does not apply to them.

`_validate_values` runs twice on append — once before the writer lease (`records.py` ~L188) and once inside `mutation_guard` (~L198). The strategy-aware check runs in both; only the guarded refusal may hold (D4).

*Why a round trip rather than allow-listing characters:* the proof is the property we want (read-back equality), it costs one serialise-and-parse per mutation on data already in memory, and it catches any future serializer quirk instead of encoding today's. *Rejected:* emitting YAML block scalars (`|`) — a second serialisation path for one field type, and the existing quoting already round-trips; a per-field manifest flag (`multiline: true`) — pushes a substrate limitation into every schema.

### D2 — Field-addressed, complete refusals

Validation collects issues instead of raising on the first: `{field, code, reason, received}` where `field` is a dotted path with array indices (`metrics[0].source`), `code` is the existing stable code, `received` is the value class (`str`, `int`, `object`, `null`). The refusal keeps the first issue's code and message as today and carries the full list in `details.issues`, plus `details.field` for the first issue so existing consumers keep working. `SCHEMA_UNKNOWN_FIELD` lists every undeclared field. `ItemSchema.validate` (`structured_collections.py` ~L680) raises on the first failure, but `_validate_field_value(name, value, spec)` (~L2535) is already a per-field entry point, so the aggregating wrapper runs it field by field with no refactor of the schema validator.

### D3 — Item-key remediation

Before UUID normalisation, a non-UUID `item_key` is compared with the candidate's natural-key field values. If it equals one of them, or the candidate's natural key is complete, the `INVALID_RECORD_ID` refusal carries `details.natural_key` (declared field names), `details.received`, and a remediation sentence: `item_key` is the internal UUID identity; omit it and identity derives from the natural key. `describe` gains the same sentence in its identity section. Nothing about identity derivation changes.

### D4 — Held records as human-owned files beside the items

A refusal for a candidate-content reason (D1, D2 codes) holds the candidate by default. Shape:

- Path: `<collection dir>/Held/<held_id>.md`, where the collection dir is `Path(manifest.path).parent` and `held_id` is the derived item key when the natural key is complete (so re-holding the same candidate replays onto one file) and a fresh UUID otherwise. `Held` has one portable spelling, like item filenames. `storage.source` is vault-relative (`structured_collections._vault_relative_path`, ~L2815) and may legally be `.`, which would place `Held/` inside the source the adapter walks and move the container hash; `hold_candidate` therefore refuses when the held directory resolves at or under `storage.source`, and the soft-fail rule below returns the original refusal plus a warning. No new error class, no new gate.
- Timing: the hold hangs off the refusal raised inside the guarded mutation boundary, not the pre-lease validation pass, so a hold never fires outside the boundary.
- Profile: holding is enabled only for the Records profile in this change. The shared writer refuses without a file for any other semantic profile, because Planning has no `held`/`hold`/`discard` arguments, drops `details`, and reports no coverage; a held file it cannot disclose or remove would be an undisclosed vault write. Planning adopts the arguments in a later change.
- Markdown-log grammar tokens (the row delimiter, the heading separator, the note bracket) are representability issues of that strategy exactly like line breaks: they are collected in the validation pass with the field path, refuse `UNREPRESENTABLE_RECORD_VALUE`, and hold like any other candidate-content refusal. The render layer's own checks remain as a last line of defence but are never the first to fire.
- `hold=false` combined with `held=` is refused naming the argument: a resume always re-holds on a repeated refusal, so the two cannot be combined coherently.
- Candidate encoding: the fenced JSON body encodes non-finite floats through a tagged form (`{"__float__": "NaN"}` and the infinities) that the resume path restores, so a `SCHEMA_FIELD_TYPE` refusal on such a value is still holdable. A candidate object whose shape equals a tag (a one-key `__float__` or `__escaped__` mapping) is wrapped as `{"__escaped__": <object>}` on encode and unwrapped literally on decode, so caller data can never be mistaken for a marker and the body stays lossless by construction.
- Markdown-log emptiness (an empty heading string, an empty or whitespace-only note) is judged in the same validation pass as the grammar tokens, with the field path, and holds; the render layer's own checks stay as the last line of defence.
- Discard receipt: `discard` returns a Records mutation receipt with `operation: discard`, `outcome: discarded`, the removed held reference and path, and no audit correlation (the hold never entered the chain). The shipped receipt validator accepts that shape explicitly, so the compact terminal projects its fields and affected path like any other receipt.
- Frontmatter (all single-line, generated): `type: held-record`, `collection_id`, `held_id`, `attempted_action` (`append`|`update`), `target_item_key` (update only), `held_at`, `why`, `candidate_sha256`, `diagnostics` (the D2 issue list).
- Body: one fenced `json` block containing the exact candidate — `item` (or `changes` and `delete_fields`), `body`, and the caller's guards — so the payload is lossless by construction and independent of the very representability rules that refused it.
- Resume: `append`/`update` with `held=<held_id>` loads the file (must live under this collection and carry its `collection_id`), applies shallow overrides from `item`/`changes` (`null` removes a field), and runs the ordinary guarded path. Success commits the item and removes the held file inside the same mutation boundary; if removal fails after the item is committed, the response warns `HELD_CLEANUP_FAILED` and the item stands. A repeated refusal rewrites the held file in place with the new diagnostics and returns the same `held_id`.
- Discard: new action `discard` with `collection`, `held`, `why`; removes the file and records nothing in the audit chain (the hold never entered it).
- Opt-out: `hold=false` on `append`/`update` restores refuse-without-file.
- Soft-fail: if writing the held file raises, the original refusal is returned unchanged with a warning; a hold never produces a second error.
- Invisibility: the directory is outside `storage.source`, so adapter reads, queries, audit markers and recall ignore it without new exclusion code; tests pin that.
- Disclosure: held candidates pass through the same per-item release filter as items before inspection or inventory counts them (the cross-audience count leak caught in #758 review is the reason).

*Why files, not review-state:* the candidate is user data — an observation the user made — and the doctrine keeps user data in human-owned Markdown; a server-internal store would hide it from Obsidian and from a vault backup. *Why a new action for discard rather than a flag on update:* `update` with `discard=true` would be an update that updates nothing; a closed action reads better in `describe` and in the argument matrix. *Rejected:* holding under `Items/` with a marker (would need adapter, audit and recall exclusions and would be counted by anything that forgets them); silently committing a best-effort item (violates structured-only canonical values).

### D5 — Coverage in inspection and inventory

`inspect_collection` gains `coverage: {committed, held, held_refs, unreadable}` where `held` is the exact count of authorized held files (counted from the directory listing, never capped by the reference bound), `unreadable` counts held files that could not be loaded rather than hiding them in a smaller number, `held_refs` is bounded (20) and each entry carries `held_id`, `held_at`, `attempted_action` and a one-line diagnostics summary that never carries candidate values — for `SCHEMA_UNKNOWN_FIELD` the summary states the number of undeclared fields, not their names, since a caller-supplied key may be a value in the wrong position. `inventory_collections` gains `committed` and `held` per collection, taken from the same governance-filtered census inspection performs plus one listing of `Held/`; its docstring states that it performs that bounded census, and the held count never parses candidate payloads. A round-trip failure of the whole candidate frontmatter reports `details.scope: "frontmatter"` and omits `details.field` rather than naming an empty field.

### D6 — Contract text, not new doctrine

The scaffold references `planning-records.md` and `mutation-results.md` (and the md5-identical plugin copy, hosted renders via the existing regeneration scripts) gain one sentence: a refused Record write is held, the response names the field, fix and resume by `held` reference; do not preserve diagnostic breadcrumbs as Evidence. Bootstrap text is unchanged in this change.

## Risks / Trade-offs

- [Held files accumulate when nobody resumes them] → inspection lists them with age; `discard` exists; a stale-held finding belongs to the follow-on change's coverage family, not here.
- [Round-trip comparison false negatives on typed fields (dates, numbers) if compared as raw strings] → compare after the schema's own normalisation on both sides, using the adapter's reader; tests cover date, datetime, integer, nested object and array fields.
- [Different clients ignore `held`] → the refusal is unchanged and still actionable; `held` is additive.
- [Tool-surface fingerprint and argument-matrix pins move] → the pin is `src/exomem/tool_surface_contract.json` (currently `sha256 ebc02dd769a14cc9e962fdfaf53138ed2bad0a3d74f9c54ea940e529513d26c7`, `tool_count 29`), regenerated together with `tests/fixtures/mcp_tool_schemas.json` by `scripts/dump-tool-schemas.py`; re-pin deliberately with the reason recorded; the matrix tests assert `held`/`hold`/`discard` only on the actions that accept them. No test pins an absolute compact-bootstrap byte size (`tests/test_bootstrap.py` pins only a session-to-compact ratio), so "unchanged" is a value to measure and record, not an existing pin at risk.
- [The scoped suites error at baseline on this machine] → `tests/conftest.py` (~L759) asserts the platform default state root is untouched across each test, and the user's live personal service writes into `~/.local/state/exomem/state` continuously (measured: 365 passed, 135 errors, 0 failures, every error that guard). The guard reads `state_paths.platform_default_state_root()`, which honours `XDG_STATE_HOME` on POSIX, so local runs set `XDG_STATE_HOME` to a scratch directory; this moves the watched root, never the service. CI is unaffected.
- [A candidate that is unrepresentable in frontmatter is also unrepresentable in the held file] → the held frontmatter carries only generated single-line fields; the candidate lives in a fenced JSON body.
- [Planning items keep the old strict rule until they adopt the arguments] → D1 applies to both profiles automatically since it lives in the shared validator; only the hold/resume/discard surface is Records-first.

## Migration Plan

Additive; no data migration. Deploy through the existing local service upgrade and hosted promotion. Rollback is a plain revert: held files are inert Markdown of a type nothing else reads. After deploy, live repair of the Public Posts v2 items is operational work through `record_memory` (append the 2026-09-09 reply with real line breaks; update the 2026-09-08 item's escaped value), not part of this change.
