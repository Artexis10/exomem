## 1. Dependency And Contract Baseline

- [ ] 1.1 Confirm `add-first-class-semantic-language` implementation and delta specs are present on the target branch; land or sync that parent before archiving this follow-on.
- [ ] 1.2 Add failing contract tests that pin one vault-independent semantic-authoring object with exact compact/rich grammar, applicability/exemptions, stable findings, version, and deterministic content digest.
- [ ] 1.3 Implement the canonical semantic-authoring contract and concise/expanded deterministic renderers without reading a vault or invoking a model.

## 2. Rich Parser Semantics

- [ ] 2.1 Add failing parser-matrix tests for same-level and shallower boundaries, deeper unknown and recognized headings, unknown structural parents, fences, built-in aliases, and repeated byte-stable parsing.
- [ ] 2.2 Add failing tests for whitespace-, metadata-, relation-, and descendant-heading-only rich blocks producing `empty_rich_unit` and no normalized/indexable unit.
- [ ] 2.3 Add failing tests proving normalized spans do not overlap: compact-shaped bullets and recognized nested headings inside a rich block remain body content, while compact rows under a separate `## Observations` section remain independent units.
- [ ] 2.4 Implement heading-hierarchy parsing, substantive-body validation, empty-unit exclusion, and non-overlapping compact/rich normalization in the shared parser.
- [ ] 2.5 Increment parser/sidecar schema versions and add rebuild/reconcile tests proving derived lexical, vector, graph, pack, and count state refreshes without Markdown rewrites.
- [ ] 2.6 Add stale anonymous reference/fingerprint and stable authored-anchor migration tests for units whose spans change under hierarchy parsing.

## 3. Minimum-Unit Applicability

- [ ] 3.1 Add table-driven failing tests for exact `compiled_intent = canonical_compiled_destination(path) OR normalized_type in COMPILED_TYPES`, path/type mismatch failures, and `requires_semantic_unit(after_state)` across all six types, active/inactive lifecycles, writable/read-only scope, and every explicit exemption.
- [ ] 3.2 Implement compiled intent, structural path/type matching, and the one shared applicability predicate using access tier, activation exclusions, and lifecycle; reject missing/invalid/mismatched compiled frontmatter instead of falling through to arbitrary Markdown.
- [ ] 3.3 Add failing evaluator tests that compact or non-empty rich form satisfies the minimum, empty rich form does not, category remains open, and unit coverage stays independent from relation disposition.
- [ ] 3.4 Implement `missing_semantic_unit` in the pure semantic evaluator with compact and rich remediation and inactive-draft warning behavior.

## 4. Write-Path And Lifecycle Enforcement

- [ ] 4.1 Add failing end-to-end tests for new active typed creation, replacement successor, inactive-to-active transition, and `validate_only` non-mutation.
- [ ] 4.2 Add failing Tier-2 create/overwrite/append tests using complete resulting Markdown, including compiled-route frontmatter bypass attempts and structural-only exemptions for non-compiled documents.
- [ ] 4.3 Add failing adoption-commit tests proving a proposal cannot write an active compiled page with no valid unit while preserved sources and proposal state remain unchanged.
- [ ] 4.4 Wire all creation, replacement, activation, Tier-2, and adoption commit paths through the shared predicate/evaluator with no facade-local weaker check.
- [ ] 4.5 Add legacy tests for non-worsening unrelated edits, refusal to remove the final unit from a post-activation page, move-stable grandfathering, direct-editor preservation, posthoc debt, and idempotent repair.
- [ ] 4.6 Extend deterministic write feedback and audit/review output with valid compact/rich counts, `missing_semantic_unit`, `empty_rich_unit`, source spans, and actionable remediation.

## 5. MCP And Generated Product Surfaces

- [ ] 5.1 Add failing bootstrap tests proving compact/full/diagnostics profiles expose identical normative authoring version/digest fields and remain byte-stable and vault-content blind.
- [ ] 5.2 Project the canonical object into every bootstrap profile, keeping expanded examples additive and non-normative.
- [ ] 5.3 Add failing registry/fidelity tests for `remember`, `replace_memory`, `observe_memory`, applicable `edit_memory` transitions, and `manage_memory_file` create/overwrite/append descriptions, including exact compact syntax, compact-versus-rich choice, minimum predicate, Tier-2 remediation, and stable findings.
- [ ] 5.4 Render concise authoring guidance from the registry into MCP, REST, CLI help/JSON, OpenAPI, generated capability docs, and committed schema fixtures; regenerate only intentional diffs.
- [ ] 5.5 Add an MCP-only acceptance test in a clean environment proving tool schemas plus default bootstrap are sufficient to distinguish category/tag/kind, choose compact or rich form, satisfy the active-page rule, and remediate a refusal.

## 6. Templates, Skills, And Plugin Packaging

- [ ] 6.1 Update every active compiled-note documentation template with a fenced generic compact example and every generated candidate/proposal with a deliberately non-parseable `## Observations` fill-in row plus the non-empty rich alternative; prove untouched candidates fail `missing_semantic_unit`.
- [ ] 6.2 Add contract-render parity tests for the core scaffold skill and every workflow skill that can compile, replace, or curate an active compiled note.
- [ ] 6.3 Update the hand-authored generic core/workflow skills so the full minimum contract appears at each standalone authoring boundary without repository-only references.
- [ ] 6.4 Enforce that every separately installable authoring workflow archive embeds the complete concise contract or fails packaging; do not accept a reference to an absent core skill.
- [ ] 6.5 Regenerate filesystem installs, generic uploadable archives, and the committed plugin from the scaffold while preserving sibling workflow-skill layout and plugin-tree parity.
- [ ] 6.6 Add clean-install acceptance tests for the plugin, a default filesystem install, and each standalone authoring workflow archive with repository files, pre-existing personal skills, and bootstrap unavailable.
- [ ] 6.7 Build and unpack wheel, sdist, plugin output, filesystem install, and every generic skill archive; verify expected contract bytes, version, digest, and support files in each applicable artifact.

## 7. Public Privacy And Synthetic Evidence Gates

- [ ] 7.1 Define the public-artifact inventory covering package source, plugin/marketplace, docs, OpenSpec, tests, fixtures, examples, example-bearing scripts, generated schemas/docs, wheel/sdist, filesystem installs, and skill/plugin archives.
- [ ] 7.2 Extend privacy validation to unpack archives, inspect member names and supported text, require explicit provenance handling for binary/unknown formats, and fail new distributable formats until coverage is declared.
- [ ] 7.3 Redact privacy diagnostics to rule, file, and line only; add tests proving matched source content is never echoed and remediation genericizes/removes content instead of expanding a private-token allowlist.
- [ ] 7.4 Replace or create every authoring regression example from a committed synthetic corpus/generator and test that no live vault is read, copied, transformed, or tokenized by public builds.
- [ ] 7.5 Isolate explicit personalized packaging to untracked private output and test that it cannot feed plugin sync, public docs/schemas, fixtures, wheel/sdist, or generic archives.

## 8. Verification And Delivery

- [ ] 8.1 Run focused parser, semantic-unit, semantic-contract, lifecycle-writer, Tier-2, adoption, bootstrap, command-fidelity, template, package-sync, built-artifact, and privacy tests with embeddings disabled.
- [ ] 8.2 Run Ruff on changed Python, generated-surface checks, plugin sync, scaffold/privacy leak gates, strict OpenSpec validation, and the complete lean test suite; record any unrelated platform baseline separately.
- [ ] 8.3 Have an independent reviewer verify every scenario, with special attention to compact/rich non-duplication, exact predicate exemptions, legacy/move safety, standalone distribution, archive inspection, and private-data isolation.
- [ ] 8.4 Update public authoring/deployment/release documentation from the canonical contract and record the parser/index rebuild plus behavior-tightening migration notes without making product-superiority claims beyond evidence.
