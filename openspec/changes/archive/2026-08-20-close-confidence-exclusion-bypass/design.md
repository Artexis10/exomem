# Design — close-confidence-exclusion-bypass

## Context

`vault.EXCLUDED_FRONTMATTER_FIELDS` is a frozenset of three names with a pure advisory
reason function beside it. It never raises; each caller converts the reason into its
own error. Two callers do: `create_file` (dict parameter only) and
`set_frontmatter_field`. Everything else that can accept an authored frontmatter key
does not.

The interesting constraint is not *whether* to fence but *where*. The collection
manifest parser is the natural-looking choke point and is the one place the fence must
never go, because it is on the read path. This document records that reasoning so it is
not "simplified" later.

## Goals / Non-Goals

**Goals:**

- One doctrine, one refusal token, across every governed authored-input surface.
- Close all three reachable bypasses, including a manifest hand-authored through
  `manage_memory_file` — without which a Records-side fence is decorative.
- Keep every existing artifact readable, queryable, rebaselineable, and repairable.
- Tell an agent the rule before it authors, through the contract it already reads.

**Non-Goals:**

- No tool-docstring edits, and therefore no movement of the pinned MCP schema baseline
  or the packaged discovery fingerprint.
- No change to the membership of the excluded set, and no `auto_*` prefix rule.
- No new audit category, no new error namespace, no governance-plane change.
- No fence on stored state, merged values, or field deletion.

## Decisions

**1. Fence authored input, never stored state.** The check runs on what a caller just
supplied — the assembled file text, the proposed manifest, the `item` mapping, the
`changes` mapping. It never runs on values read back from disk. Consequences that are
intended, not oversights: an unrelated update to a grandfathered item leaves its
existing excluded value untouched, because the fence sees `changes` and not the merged
result; and `delete_fields` is never fenced, because removing the field is the
remediation.

**2. The manifest parser is off limits, and this is the load-bearing decision.**
`_parse_schema` is reached by `load_manifest` and `parse_manifest_bytes`, and through
them by collection discovery, every collection resolution, every item read, the
recall-candidacy check, and the guarded manifest load that precedes *every* mutation.
`CollectionError` subclasses `ValueError`, and the recall-candidacy check catches
`ValueError` and returns `False`. A refusal in the parser would therefore remove a
grandfathered collection from recall **silently** — no error, no finding, no log — and
would simultaneously break the revise and rebaseline that could repair it. One poisoned
manifest would also break discovery for every other collection, because discovery
raises on the first bad manifest inside its loop. The fence goes at the write entry
points that call the parser, immediately after the parse returns.

**3. Grandfathering follows the activation-manifest precedent.** Existing violations
are review candidates, not blocking findings. The audit emits a `warn`-severity finding
under the existing `frontmatter_compliance` category — deliberately reusing the
category so the category registry is untouched — for an excluded top-level key and for
a `type: collection` page declaring one under `item_schema.fields`. The finding carries
the ordered remediation, and the order matters: delete the field from every item first,
then revise the manifest. The reverse order fails, because revision validation checks
the proposed schema against stored record values.

**4. `create_file` must understand collection manifests.** A top-level-key check would
not see `item_schema.fields.confidence`, so a manifest hand-authored through
`manage_memory_file` would create a fully operable collection that the Records fence
never sees — its audit chain simply starts at `baseline`. When the parsed frontmatter
declares `type: collection`, the guard also walks `item_schema.fields`.

**5. The `create_file` guard parses non-strictly.** A non-strict parse returns an empty
mapping for a file with no frontmatter and for malformed YAML, so the guard adds zero
new `INVALID_FRONTMATTER` refusals on the overwrite path. A strict parse would newly
refuse writes that succeed today — a real regression bought to prevent a theoretical
one. The residue is that syntactically invalid YAML containing `confidence:` slips
through; nothing downstream reads it as `confidence` either.

**6. One error code across three exception types.** `EXCLUDED_FIELD` already reaches
the MCP boundary identically from both existing surfaces. The neighbouring collection
codes mean different things — `RESERVED_RECORD_FIELD` is "collides with a system
field", `SCHEMA_UNKNOWN_FIELD` is "not declared" — and neither is "declared but
forbidden by doctrine." The code becomes a named constant beside the frozenset, and the
collection raise carries `details` naming the offending field, matching the
self-remediating error contract.

**7. Doctrine outranks representation in revision validation.** The fence runs before
the immutable-representation check, so a manifest violating both reports
`EXCLUDED_FIELD`. This is an intentional ordering change.

**8. Disclosure rides the manifest authoring contract, not docstrings.** The contract
is runtime response data surfaced by `record_memory(action="describe")`, so adding the
excluded names to the `item_schema.fields` node satisfies the requirement that a client
with no repository or fixture access can author a valid manifest from `describe` alone
"without any guessed field name" — while leaving the pinned tool schemas byte-identical.

## Risks / Trade-offs

- [A future reader "simplifies" the fence into `_parse_schema`] -> Decision 2 above,
  plus an anti-regression test asserting a grandfathered collection remains a recall
  candidate. That test is the guardrail; it must not be removed as unrelated.
- [The `type: collection` branch looks like scope creep and gets cut] -> Without it the
  Records fence is decorative. If scope must be cut, cut the contract disclosure first.
- [Error-ordering change breaks a test asserting the immutability code on an input that
  violates both] -> Run the record lifecycle and mutation-matrix suites deliberately.
- [Revising a grandfathered collection's unrelated fields is blocked until cleanup] ->
  Accepted. Rebaseline stays open so nothing is stuck, and the audit finding states the
  ordered remedy. The alternative — fencing only the delta of fields added versus the
  current schema — is more code and more state, and it lets a grandfathered field
  survive an explicit re-authoring of the contract.
- [Grandfathering tests must plant a manifest on disk directly, because a governed
  create refuses it after this change] -> Unavoidable and correct. The test docstrings
  say so, so nobody converts them into governed calls and silently guts the coverage.
