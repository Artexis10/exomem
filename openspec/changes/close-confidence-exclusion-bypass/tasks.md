# Tasks — close-confidence-exclusion-bypass

## 1. Pure-Logic Unit Tests First

- [ ] 1.1 Add `tests/test_schema_field_exclusion.py` covering a new
      `vault.first_excluded_field(names)`: it reports the offending name and its
      reason, returns `None` for clean input and for an empty iterable, skips
      non-string names without raising, and honors the existing casefold/strip
      normalization (`Confidence`, `DECAY_AT`, `" expires_at "`). Run RED and record
      the verbatim failures.
- [ ] 1.2 Assert `vault.EXCLUDED_FIELD_CODE == "EXCLUDED_FIELD"` and that it is the
      code both `create_file` and `set_frontmatter_field` raise, so a later rename
      cannot silently split the namespace.
- [ ] 1.3 Add the grandfathering anti-regression tests, which MUST pass both before
      and after the fence: a manifest planted on disk declaring `confidence` stays
      loadable and queryable, its items stay readable, and
      `recall_policy.is_recall_candidate` returns True for it. State in the docstring
      that the manifest is planted directly because a governed create refuses it after
      this change. Run GREEN and record the baseline.
- [ ] 1.4 Add anti-regression tests that the repair path stays open: an item's
      excluded field can be removed with `update_record(delete_fields=...)`, the
      manifest can then be revised without it, and `rebaseline_collection` is never
      refused with `EXCLUDED_FIELD`. Run GREEN and record the baseline.

## 2. Bypass Regression Tests

- [ ] 2.1 `manage_memory_file` raw-content bypass: `create_file` with
      `frontmatter=None` and an embedded `confidence:` refuses with `EXCLUDED_FIELD`
      and writes no bytes. Run RED.
- [ ] 2.2 `overwrite=true` bypass: overwriting an existing Markdown note with raw
      content carrying `decay_at:` refuses and leaves the prior bytes intact. Run RED.
- [ ] 2.3 Hand-authored manifest through `manage_memory_file`: a
      `Knowledge Base/Records/<name>/_collection.md` declaring `confidence` under
      `item_schema.fields` refuses. Run RED. This is the test that proves the Records
      fence is not decorative.
- [ ] 2.4 Records manifest authoring: `create_collection`, `validate_collection_create`,
      `revise_collection`, and `validate_collection_revision` each refuse a manifest
      declaring an excluded name in `item_schema.fields`, and the on-disk manifest is
      unchanged. Run RED.
- [ ] 2.5 The second declaration surface: a manifest declaring the excluded name as the
      Markdown-log note field refuses on create. Run RED.
- [ ] 2.6 Planning manifest authoring: `planning.create_collection` refuses the same
      manifest, proving both profiles ride one fence. Run RED.
- [ ] 2.7 Item authoring against a grandfathered collection:
      `append_record(item=...)` and `update_record(changes=...)` refuse an excluded
      key, while `update_record(delete_fields=...)` still succeeds. Run RED.
- [ ] 2.8 `expires_at` coverage across `set_frontmatter_field`,
      `create_file(frontmatter=...)`, `create_file` raw content, and
      `records.create_collection`. Confirm `tests/test_tier2.py` still passes untouched.

## 3. Shared Exclusion Primitive

- [ ] 3.1 Add `EXCLUDED_FIELD_CODE` and a pure, non-raising
      `first_excluded_field(names) -> tuple[str, str] | None` to `src/exomem/vault.py`
      beside `EXCLUDED_FRONTMATTER_FIELDS`. Add one comment naming the deferred
      `auto_*` gap and citing the scaffold reference that documents it.
- [ ] 3.2 Replace the bare `"EXCLUDED_FIELD"` literals in `create_file.py` and
      `set_frontmatter_field.py` with the constant; behavior unchanged.

## 4. Close The Raw-Content And Overwrite Paths

- [ ] 4.1 Guard the assembled file text for every Markdown write in `create_file`,
      after the text is assembled and before the stable-identity guard, using a
      NON-strict frontmatter parse so no file that writes today begins failing with
      `INVALID_FRONTMATTER`.
- [ ] 4.2 Extend that guard so that when the parsed frontmatter declares
      `type: collection`, an excluded name declared under `item_schema.fields` also
      refuses.
- [ ] 4.3 Keep the existing `frontmatter` dict guard in place; it refuses before path
      resolution and an existing test asserts that ordering.

## 5. Close The Manifest-Authoring Path

- [ ] 5.1 Add a private helper in `records.py` that refuses a parsed manifest
      declaring an excluded name in `item_schema.fields` or as the Markdown-log note
      field, raising `CollectionError` with the shared code and `details` naming the
      field.
- [ ] 5.2 Call it from the collection-create preflight immediately after the manifest
      parse, and from revision validation immediately after its parse and BEFORE the
      immutable-representation check so doctrine outranks representation.
- [ ] 5.3 Do NOT call it from `_parse_schema`, `_manifest_from_frontmatter`,
      `load_manifest`, `parse_manifest_bytes`, `validate_storage_contract`, or
      `_validate_values`. Confirm by test that `rebaseline_collection` reaches neither
      fence, so a grandfathered collection keeps its repair path.

## 6. Close The Item-Authoring Path

- [ ] 6.1 Fence the caller-supplied `item` in `append_record` before collection
      resolution, and the caller-supplied `changes` in `update_record` before the
      writer lease is taken. Never fence `delete_fields`, merged values, or stored
      values.
- [ ] 6.2 Confirm by test that Planning `add`, `update`, and `triage` inherit the fence
      with no Planning-side code change.

## 7. Surface, Do Not Block, Existing Violations

- [ ] 7.1 Extend the audit's frontmatter-compliance check to emit a `warn`-severity
      finding for any page carrying an excluded top-level key, and for any
      `type: collection` page declaring one under `item_schema.fields`. Reuse the
      existing category so the category registry is untouched.
- [ ] 7.2 Give each finding a proposed fix naming the ordered remediation: delete the
      field from every item first, then revise the manifest.
- [ ] 7.3 Assert no such finding carries `error` severity — review candidate, never
      blocking.

## 8. Agent-Facing Contract

- [ ] 8.1 Disclose the excluded names on the `item_schema.fields` node of the manifest
      authoring contract so `record_memory(action="describe")` tells a client before it
      authors an invalid manifest, satisfying the describe-alone authoring requirement.

## 9. Verification

- [ ] 9.1 `openspec validate close-confidence-exclusion-bypass --strict` and
      `openspec validate --specs --strict`.
- [ ] 9.2 Run the new file plus `tests/test_tier2.py`, `tests/test_record_lifecycle.py`,
      `tests/test_record_mutation.py`, `tests/test_structured_collections.py`,
      `tests/test_planning_mutation.py`, `tests/test_records_recall_policy.py`,
      `tests/test_audit.py`, and `tests/test_record_presentation_contract.py`.
- [ ] 9.3 Lean suite `uv run python -m pytest -q` and the latency gate
      `uv run python -m pytest tests/test_latency_gate.py -q` both green;
      `uvx ruff check .` clean on changed files.
- [ ] 9.4 `git diff --exit-code tests/fixtures/mcp_tool_schemas.json` and
      `git diff --exit-code src/exomem/tool_surface_contract.json` both clean — the
      proof no docstring drifted into this change.
- [ ] 9.5 Independent reviewer pass over the actual diff, focused on read-path
      regressions, grandfathered-collection availability, and the error-ordering change.
