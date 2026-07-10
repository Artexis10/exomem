## 1. Dependency and compatibility baseline

- [x] 1.1 Confirm merged PR #182 provides stable references, unified context, and `schema_memory` as the implementation foundation
- [ ] 1.2 Add a golden snapshot of every current relation definition, parser acceptance rule, edge direction, origin, and graph-context result
- [ ] 1.3 Add migration tests proving existing Markdown and graph fixtures remain behaviorally identical after registry consolidation

## 2. Core and extension registry

- [ ] 2.1 Implement typed core relation definitions and one versioned packaged registry consumed by semantic parsing and graph indexing
- [ ] 2.2 Remove duplicated relation enums and add parity checks that every consumer resolves the same core registry
- [ ] 2.3 Implement generic extension-registry loading, namespaced key validation, core-parent mapping, scope checks, aliases, inverse metadata, and deprecation
- [ ] 2.4 Add hash-aware registry caching plus stable validation findings for collisions, invalid parents/scopes, inverse cycles, and bad replacements
- [ ] 2.5 Add the leak-guarded empty extension registry and generic guidance to the shipped scaffold

## 3. Registry-aware graph state

- [ ] 3.1 Bump the graph sidecar schema and persist raw/canonical relation identity, parent, registry status/version/hash, and existing source provenance
- [ ] 3.2 Preserve syntactically valid unregistered observations as semantically inert derived edges with resolved or placeholder targets
- [ ] 3.3 Restrict unknown capture to explicit typed intent and add regression tests proving navigation bullets do not create ontology or attention noise
- [ ] 3.4 Resolve aliases, deprecated keys, and scope violations deterministically without rewriting Markdown
- [ ] 3.5 Extend opt-in graph governance audit and reconcile for unknown, deprecated, out-of-scope, invalid, and registry-hash drift
- [ ] 3.6 Add rebuild, edit, move, watcher, delete, and registry-change tests for incremental and full graph freshness

## 4. Corpus relation governance

- [ ] 4.1 Extend `schema_memory` with backward-compatible relation-registry subject selection and structured reviewed proposals
- [ ] 4.2 Implement scoped frequency/example inference for core, extension, alias, deprecated, scope-violating, and unregistered observations
- [ ] 4.3 Implement atomic expected-hash persistence that refuses incomplete semantics, collisions, invalid scopes, or observed-key deletion
- [ ] 4.4 Implement registry validation and diff against corpus reality or another reviewed proposal
- [ ] 4.5 Add default-off response-only optional parent/description suggestions with attribution and soft-fail tests

## 5. Traversal profiles and unified context

- [ ] 5.1 Implement immutable `epistemic`, `provenance`, `causal`, `decision`, and `all` built-in profiles with deterministic priorities and bounds
- [ ] 5.2 Implement governed custom profile loading, built-in extension, add/remove rules, parent expansion, direction, caps, validation, and cache hashing
- [ ] 5.3 Apply profile resolution and narrowing filters to bounded graph traversal without changing graph rows or default `find` ranking
- [ ] 5.4 Extend unified context with resolved profile, registry metadata, extension ancestry, unknown/scope warnings, excluded counts, and truncation
- [ ] 5.5 Add cross-domain golden tests proving precise extensions remain queryable through portable core families and different lenses remain read-only

## 6. Product surfaces and proof

- [ ] 6.1 Register every new schema/context parameter once and regenerate MCP, REST, CLI, OpenAPI, annotations, capability docs, and fidelity fixtures
- [ ] 6.2 Update the generic skill guidance for core versus extension semantics, proposal-first adoption, deprecation, and traversal lenses
- [ ] 6.3 Add an installed-wheel product E2E that registers reviewed domain extensions, rebuilds after restart, and queries epistemic/provenance/causal profiles
- [ ] 6.4 Run focused and full lean suites, registry/profile golden checks, Ruff, targeted typing, generated-doc checks, and strict OpenSpec validation
- [ ] 6.5 Run dry-run inference over a broad real corpus and document proposed vocabulary without shipping personal extensions in the scaffold
