# Close the confidence-exclusion bypass

## Why

Exomem's substrate constraint is stated flatly in `openspec/config.yaml`: "No
confidence/authority floats on notes." The enforcement primitive exists —
`vault.EXCLUDED_FRONTMATTER_FIELDS` (`confidence`, `decay_at`, `expires_at`) plus
`excluded_frontmatter_reason()` — but only two call sites consult it: `create_file`'s
`frontmatter` dict parameter and `set_frontmatter_field`. The documented stance is
therefore enforced at two of N governed write surfaces.

Three authored-input paths accept an excluded field today:

1. `manage_memory_file` with `frontmatter` omitted writes `content` verbatim, embedded
   YAML included. `create_file`'s docstring advertises this ("the caller is responsible
   for any frontmatter already in it") and never mentions excluded fields.
2. The same path with `overwrite=true` on an existing Markdown file, which skips even
   the compiled-type frontmatter parse.
3. A collection manifest — Records or Planning — may declare `confidence` in
   `item_schema.fields` or as the Markdown-log note field. Manifest text is written
   byte-for-byte and unknown keys are silently preserved. Worse, a manifest can be
   hand-authored straight through `manage_memory_file`, so a Records-side fence alone
   would be decorative.

Item writes are already fenced by `SCHEMA_UNKNOWN_FIELD`, so an item cannot introduce
an excluded key unless a manifest declared it first. Manifest authoring is the root
cause; the renderers are not.

The audit that surfaced this graded it minor because "agents follow the docstrings."
That premise does not hold: the pinned MCP tool surface contains zero occurrences of
`confidence`, `decay`, or `EXCLUDED_FIELD`. The rule lives only in scaffold files an
agent reads if it loads the skill. The fence should close before the epistemic
primitives arrive and third-party agents start writing confidence numbers into
hypothesis and prediction units.

## What Changes

- Add a shared `EXCLUDED_FIELD_CODE` constant and a pure `first_excluded_field(names)`
  helper beside the existing frozenset, and replace the two bare `"EXCLUDED_FIELD"`
  literals with the constant.
- Refuse an excluded field on every **authored-input** surface: `create_file`'s
  assembled text for any Markdown write (covering the raw-`content` and `overwrite`
  paths, and `item_schema.fields` when the frontmatter declares `type: collection`),
  Records and Planning manifest create and revise, and the caller-supplied `item` and
  `changes` mappings on record append and update.
- Disclose the excluded names through the manifest authoring contract so
  `record_memory(action="describe")` tells a client before it authors an invalid
  manifest.
- Surface pre-existing violations as `warn`-severity `frontmatter_compliance` audit
  findings carrying the ordered remediation, never as blocking findings.

**Explicitly not changed** — each is a read path, and refusing there would make a
grandfathered collection silently vanish from recall (`CollectionError` subclasses
`ValueError`, which the recall-candidacy check swallows into `False`) and would break
the very revise that repairs it: `_parse_schema`, `_manifest_from_frontmatter`,
`load_manifest`, `parse_manifest_bytes`, `validate_storage_contract`,
`_validate_values`. `delete_fields` is never fenced, and `rebaseline_collection`
reaches neither manifest fence, so the repair path stays open.

**Non-goals.** No tool-docstring edits: they would move the pinned MCP schema baseline
and the packaged discovery fingerprint, whose ChatGPT Personal Plugin attestation is
release-blocking until that external consumer is refreshed. That is a separate change.
The `auto_*` prefix rule documented in the scaffold is also out of scope: a prefix
predicate is a different rule shape with different blast radius on foreign vault
frontmatter. No change to the excluded set's membership.

## Capabilities

### New Capabilities

- `schema-field-exclusion`: Governed writes refuse schema-excluded frontmatter field
  names on authored input, while existing artifacts stay readable and repairable and
  their violations surface for review.

### Modified Capabilities

- `structured-collections`: Item schemas are open-vocabulary except for the
  schema-excluded set, and uncertainty may not be expressed as a numeric confidence
  field.

## Impact

Additive refusal on authored input only; no data migration exists and none is needed.
Verified: zero collections in the repository declare an excluded field, and the four
`_collection.md` fixtures are clean. A grandfathered collection in a user vault stays
loadable, queryable, rebaselineable, and repairable — its items can still be cleaned
with `delete_fields`, after which the manifest can be revised. The one accepted cost
is that revising such a collection's other fields is blocked until the excluded field
is removed; rebaseline remains open, so nothing is stuck.

Pure-substrate justification: no model runs on any path this change touches. The check
is a casefolded membership test against a frozen set of three names, and the audit
finding it feeds is a deterministic string comparison over already-parsed frontmatter.

The pinned MCP tool surface does not move. `tests/fixtures/mcp_tool_schemas.json` and
`src/exomem/tool_surface_contract.json` must both be byte-identical after this change;
the manifest authoring contract is runtime response data, not pinned schema.
