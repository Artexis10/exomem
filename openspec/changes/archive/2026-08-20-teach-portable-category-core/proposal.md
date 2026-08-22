# Teach a Portable Category Core

## Why

Exomem already parses, normalizes, governs, and retrieves semantic-unit categories, but the shipped authoring surfaces do not teach agents a coherent vocabulary. The starter registry is empty and the few point-of-use examples repeatedly use one ad hoc phrase, so otherwise-correct clients invent near-duplicates instead of building a reusable semantic language.

## What Changes

- Ship a small, generic starter vocabulary that supports both semantic roles (for example decisions and constraints) and domain lenses (for example code and design).
- Project the same category guidance into bootstrap, tool descriptions, authoring feedback, the generic scaffold, and generated plugin artifacts.
- Keep categories open and portable: unknown compact categories remain valid, aliases normalize common variants, and no default ranking boost or write rejection is introduced.
- Return bounded advisory feedback after semantic writes for aliases, deprecated/replaced labels, and out-of-scope use while preserving opt-in strict saved contracts.
- Teach agents how categories, kinds, tags, and typed relations differ, with rich generic examples that encourage several observations and relations per durable note.
- Make corpus-derived vocabulary evolution reviewable and deterministic rather than silently rewriting the registry.
- Activate the teaching projection only after indexed category retrieval passes its correctness, recovery, and latency gates.

## Capabilities

### New Capabilities

- `portable-category-authoring`: Defines the starter vocabulary, open-category escape hatch, normalization rules, point-of-use teaching, advisory feedback, and reviewed evolution contract.

### Modified Capabilities

- `agent-bootstrap-contract`: Bootstrap profiles teach the same versioned semantic authoring contract without inspecting private vault content.
- `command-surface`: Generated MCP, REST, CLI, and OpenAPI descriptions and write results expose one category contract instead of drifting per surface.

## Impact

Expected implementation areas include semantic authoring and registry modules, semantic write responses, bootstrap and command metadata, the hand-authored generic scaffold and generated plugin copies, plus contract and no-private-leak tests. Existing notes and unknown categories remain valid; this is additive guidance and governance, not a closed taxonomy migration.
