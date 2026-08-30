## 1. Workflow contract core

- [ ] 1.1 Add red-first focused tests for the code-owned family registry, exact workflow v1 frontmatter schema/normalization/bounds, canonical project-key selectors, contract-wide single-owner tokens, companion sorting, stable/immutable identity, configured-KB paths, natural-case filenames and collisions, deterministic English presentation, authored-body preservation, presentation drift, safe paths, symlink refusal, and stale-write protection.
- [x] 1.2 Implement the reusable contract-family registry and the versioned `workflow` family parser, validator, canonical serializer, fingerprint, deterministic renderer, and bounded inventory model.
- [x] 1.3 Implement canonical workflow-contract storage under `_Schema/contracts/workflow/`, including guarded preview, save, refresh, direct-edit validation, lifecycle handling, and no-contract bootstrap compatibility.
- [ ] 1.4 Add red-first resolver tests covering explicit saved selection, reviewed ephemeral proposals, three-state project/domain/activity context, incomplete-context refusal, specificity, default and built-in standalone fallback, archived explicit selection, unreadable/unparseable inventory, duplicate IDs/keys, multiple defaults, scan limits, deterministic provenance, and equal-specificity ambiguity refusal.
- [ ] 1.5 Implement the pure deterministic resolver and fixed-template explanation without model calls, tool-capability claims, contract merging, external state inference, or executable user instructions.
- [ ] 1.6 Add adversarial contract tests for instruction-like data, oversized values, traversal and link attacks, malformed direct edits, unstable file ordering, bounded error disclosure, exact semantic/source fingerprints, scan exhaustion with no partial total, and byte-equivalent results when unreleased files are added.

## 2. One product surface

- [ ] 2.1 Add red-first command-schema and exact argument-matrix/result tests for `schema_memory` subject `workflow-contracts`: inventory, inspect, validate, resolve, preview, guarded save, and refresh, including stable refusal codes.
- [x] 2.2 Wire every workflow-contract operation through the registered `schema_memory` command so MCP, REST, CLI, envelopes, dry-run semantics, stale-write errors, and audit receipts remain one implementation; add selector-level lease/receipt classification while keeping the command statically mixed/write-capable.
- [ ] 2.3 Add the workflow-specific default-deny egress path and release filtering before inventory/resolution; prove an unreleased winner/tie/default is physically absent and cannot change result bytes, refusals, counts, or ambiguity sets.
- [ ] 2.4 Update public command schemas, governance selector registries, generated fixtures, help, and capability descriptions; prove the active command surface gains no new top-level product command or unsupported partial profile.

## 3. Bootstrap and portable contract

- [ ] 3.1 Add red-first bootstrap tests for immutable Planning/Records ownership, built-in standalone fallback, one released default/eight scoped summary caps, released-only totals, exact resolve route only when callable, `workflow_resolution_unavailable` on reduced profiles, empty-vault behaviour, and separation from governance and companion availability.
- [x] 3.2 Project the workflow-contract protocol through full and compact bootstrap, portable export, and knowledge packs from the code-owned machine source; update the hand-authored generic scaffold and parity pins without generating it from a vault; remove product-specific OpenSpec behaviour from normative generic guidance.
- [x] 3.3 Add a generic inactive OpenSpec companion example outside the active contract directory, with no personal vault labels or assumption that OpenSpec exists.
- [x] 3.4 Implement and test the durable migration marker before scaffold refresh: known pre-feature vaults require review, fresh vaults do not, valid markers are preserved, missing markers beside sentinels are conservative, and invalid/unsafe markers refuse without overwrite.
- [x] 3.5 Prove parity, compact-budget bounds, hand-authored scaffold genericity, portable-offline usefulness, and full/compact capability separation with focused tests.

## 4. Planning and Records feedback loop

- [ ] 4.1 Add red-first contract-projection scenarios for proactive versus explicit durable-intent capture, prominence caps, update-before-create behaviour, tentative conversation, standalone versus companion ownership, and explicit known-absent context.
- [ ] 4.2 Add red-first scenarios for observed-outcome capture into one compatible Records collection, opaque companion references, Records-to-Planning links, explicit user transitions, proposed transitions, and the prohibition on automatic completion.
- [x] 4.3 Replace privileged OpenSpec/repository wording across canonical runtime descriptions, validators, bootstrap, scaffold, and knowledge packs; broaden Planning `execution.kind` to the exact open key syntax while preserving every existing enum value as valid opaque data.
- [ ] 4.4 Implement the bounded agent-facing Planning/Records protocol and resolver decision fields needed by those scenarios without adding a server-side conversational classifier or calling a companion system.
- [ ] 4.5 Keep plan-progress and `unreflected_outcomes` as the deterministic review surfaces and add regression coverage that contracts do not bypass their governance or transition rules.

## 5. Documentation and migration

- [x] 5.1 Document standalone and companion workflows, scope resolution, safe editing in Markdown/Obsidian, ambiguity and invalid-file recovery, reviewed ephemeral overrides, and the Records feedback loop.
- [x] 5.2 Document the representation-compatible but behaviour-explicit migration and rollback path, including the migration-required refusal, current-session standalone selection, durable saved choice, and unchanged Planning/Records files.
- [x] 5.3 Document how future integrations and the hosted portal consume the stable resolver/save surface without making external tools or a portal part of this delivery.

## 6. Independent acceptance and closure

- [x] 6.1 Run focused workflow-contract, command-surface, bootstrap, Planning, Records, portability, security, and documentation tests plus lint/type checks for touched modules.
- [ ] 6.2 Run one phase-boundary full test suite, strict OpenSpec validation, the public-artifact privacy gate, and explicit mutation-tripwire cases for the new parser/resolver branch and precedence predicates; record exact evidence.
- [ ] 6.3 Have a fresh author-independent reviewer reproduce the user journeys and adversarial cases against the integrated diff; return all findings to the implementation lane and rerun the same reviewer after corrections.
- [ ] 6.4 Synchronize the delta specs, archive the completed change through OpenSpec, and rerun `openspec validate --all --strict` before delivery.
