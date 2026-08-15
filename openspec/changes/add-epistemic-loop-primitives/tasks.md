## 1. Pure-Logic Contract Tests

- [x] 1.1 Add failing parser tests for the `prediction` kind: singular and plural headings resolve to `prediction`, the core registry resolves it with core status against a registry-less vault, and a registry proposal declaring `prediction` reports `canonical_collision`.
- [x] 1.2 Add failing parser tests for `verdict` grammar: each of the five accepted values normalizes through NFKC and casefold, an unknown value and a numeric value both raise `invalid_rich_verdict` bound to the authored line, and the numeric remediation states confidence is not a stored field.
- [x] 1.3 Add failing parser tests for `check_by` grammar: a strict ISO calendar date is projected, and an abbreviated date, a timestamp, and free text each raise `invalid_rich_check_by`.
- [x] 1.4 Add failing tests that both governed keys are reserved metadata rows, so a rich block whose only rows are governed metadata still reports `empty_rich_unit`, and that a compact observation carries neither value.
- [x] 1.5 Add failing tests that one shared five-value vocabulary backs both the unit `verdict` and the experiment `outcome`.
- [x] 1.6 Add failing structured-filter compiler tests for `unit.verdict`: closed string field, casefolded operands, non-string operand rejection, `$exists` truthfulness, and an unknown `unit.*` field still rejected.
- [x] 1.7 Add failing structured-filter compiler tests for `unit.check_by` as a typed date: ordered comparisons compile, a non-date operand is refused, and `$contains` is refused.
- [x] 1.8 Add failing evaluator tests that `unit_view` normalizes `check_by` to a real date so an ordered comparison against a date operand is decidable, and omits both keys when absent.

## 2. Semantic Unit Language

- [x] 2.1 Add `prediction` to the code-owned block-type table and `predictions` to the heading alias table.
- [x] 2.2 Add the shared five-value epistemic outcome vocabulary as one module-level constant.
- [x] 2.3 Parse, validate, and project `verdict` and `check_by` onto the semantic-unit model with deterministic source-addressed diagnostics, and include both in the unit's serialized form.
- [x] 2.4 Register both keys as reserved rich metadata rows so governed metadata never counts as substantive body.

## 3. Write Contract Coverage

- [x] 3.1 Add failing write-contract tests that a page whose only unit is a substantive `## Prediction` block satisfies the minimum-unit rule, and that a prediction without a verdict raises no finding.
- [x] 3.2 Add failing write-contract tests that adding a verdict does not change the minimum-unit outcome and that no contract finding references predictions on a page without one.
- [x] 3.3 Confirm the existing shared contract needs no normative change and record the decision; do not bump the authoring contract version or its content digest.

## 4. Observe Memory Preserve-By-Default

- [x] 4.1 Add the load-bearing failing test: updating only the content of a rich unit that carries `- verdict: refuted` must keep that row. Assert on the written Markdown and on the returned unit.
- [x] 4.2 Add a failing test that an authored metadata row the parser does not interpret also survives an update verbatim.
- [x] 4.3 Add failing tests for the governed arguments: supplied values render, an explicit empty string clears, omission preserves, and governed metadata without an explicit rich kind is refused with a stable code.
- [x] 4.4 Add failing tests for the `id` argument: a valid anchor is honoured end to end, an invalid anchor is refused, and an anchor already used on the page is refused.
- [x] 4.5 Implement the merged reconstruction: partition current metadata into owned and unowned keys, re-emit owned rows from resolved arguments in canonical order, and append unowned rows verbatim.
- [x] 4.6 Extend the round-trip assertion to cover preserved rows so a dropped row fails the write.
- [x] 4.7 Wire `verdict`, `check_by`, and `id` through the command registry entry with help text stating the preserve-by-default and clear-with-empty-string contract.

## 5. Retrieval Surfaces

- [x] 5.1 Replace the single typed-date special case with a shared date-field set and add `unit.check_by` to it; add `unit.verdict` to the closed string fields and to the unit field registry.
- [x] 5.2 Canonicalize `unit.verdict` operands and runtime values by trimming and casefolding.
- [x] 5.3 Normalize `check_by` to a real date in the unit view and omit both governed keys when absent.
- [x] 5.4 Add failing tests that a unit hit carries both governed keys when present, omits them when absent, keeps `verdict` in the compact projection, and that the governed-egress semantic-unit projector registers both fields.
- [x] 5.5 Carry both values onto the semantic-unit hit and its serializers, and register both fields with the governed-egress projector.
- [x] 5.6 Add failing tests that a verdict changes no ranking signal, does not mark a unit or its parent superseded, and does not exempt a unit from page-status inheritance; then confirm no production change is required.

## 6. Note Type Contract

- [x] 6.1 Add failing note-type tests that `concluded` is an accepted experiment status, that an unknown status is still refused, and that concluded is not treated as archived.
- [x] 6.2 Add failing frontmatter-write tests that a valid `outcome` is accepted on an experiment, an invalid value is refused, `outcome` on a non-experiment page is refused, and a `confidence` field remains refused.
- [x] 6.3 Add `concluded` to the experiment status enum.
- [x] 6.4 Enforce the `outcome` enum and its experiment-only scope at the frontmatter-field write boundary.

## 7. Shipped Teaching

- [x] 7.1 Teach the experiment `concluded` status and the `outcome` enum in the scaffold frontmatter and page-type references, and restate confidence as a non-field covering `outcome` and `verdict`.
- [x] 7.2 Teach the `prediction` kind and the two governed unit-metadata keys in the scaffold page-type reference.
- [x] 7.3 Mirror both scaffold reference edits into the packaged Claude Code plugin skill copies so the two shipped copies stay byte-identical.
- [x] 7.4 Confirm the scaffold leak guard still passes over every edited file.

## 8. Regenerate The Moved Contracts

- [x] 8.1 Regenerate the pinned MCP tool schemas and the tool-surface discovery fingerprint, then review the diff and confirm it contains only the intended `observe_memory` additions.
- [x] 8.2 Regenerate the generated capability documentation.
- [x] 8.3 Record the moved MCP discovery digest as the ChatGPT Personal Plugin pending attestation, keep the rollout state awaiting refresh, and report the moved fingerprint as release-blocking.
- [x] 8.4 Re-render the derived hosted compatibility descriptor and both platform locks for the v1 and v2 candidates, verify the descriptor diff is confined to `observe_memory`'s three added arguments, and confirm every package archive stays byte-identical.
- [x] 8.5 Update the three moved entries in the v1 release-identity fixture and record the reasoning for moving a frozen candidate's derived identity in `design.md`.
- [x] 8.6 Re-render the `claude-connector` and `claude-plugin` directory packets, which embed the tool schemas and the descriptor digests; leave `openai-plugin` alone because it was already stale on `origin/main`.
- [x] 8.7 Repoint the kind-mapping fairness evidence, whose line-number citations into `page-types.md` the scaffold teaching shifted.

## 9. Review Corrections

- [x] 9.1 Scope the tool help's preservation claim to the governed keys plus unowned rows and state replace-on-omit for `tags`, `context`, and `relations`, because the help is digested into the shipped surface and the unscoped claim asserted the opposite of the behaviour. Pin both halves against the generated schema.
- [x] 9.2 Enforce the `outcome` enum at the creation frontmatter boundary as well, so the no-stored-confidence doctrine is not defended for `confidence` and abandoned for its categorical twin. Put the policy beside `excluded_frontmatter_reason` so the two boundaries cannot drift, and test both routes.
- [x] 9.3 Make the compact-conversion refusal name only exits the caller can actually take, and test the clause three ways including the remediation's own path.
- [x] 9.4 Record why `to_dict` emits null where the presence-sensitive serializers omit, and stop calling key-normalized preservation "verbatim".

## 10. Targeted Verification

- [x] 10.1 Run the new tests plus the adjacent semantic language, write contract, observe memory, unit recall, structured filter, note, audit, connector guardrail, and scaffold leak suites.
- [x] 10.2 Run **all four** shard groups against the final tree, not one. Both tests this change calls its load-bearing assertions live in groups 2 and 3, so a group-1-only run would claim coverage it did not provide.
- [x] 10.3 Confirm any residual shard failure reproduces on unmodified `origin/main` before attributing it elsewhere.
- [x] 10.4 Lint every changed file and validate the change strictly.
