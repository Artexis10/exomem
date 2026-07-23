## Why

Exomem has a stronger semantic language than plain Markdown, but the product does
not yet make that language unavoidable at the point of authoring. A client can
create an active compiled note with no usable semantic units, while MCP-only
clients, installed skills, and the packaged plugin receive different amounts of
the authoring contract.

## What Changes

- Require every newly created, replaced, or activated active compiled note to
  contain at least one valid, non-empty semantic unit. A compact observation
  under `## Observations` is the default lightweight form; a valid rich unit can
  satisfy the invariant when a governed kind or typed unit relations are needed.
- Apply that rule through the shared semantic write boundary to typed writers,
  Tier-2 Markdown creation/overwrite/append, edit/activation, adoption
  compilation, and other in-process compiled-note mutation paths. Existing pages remain grandfathered, and direct
  editor changes are preserved and surfaced as posthoc debt.
- Make rich-block parsing heading-hierarchy aware so nested subsections remain
  part of their parent rich unit and an actually empty recognized block produces
  a stable finding instead of a misleading empty unit.
- Define one versioned, generic semantic-authoring contract and project its exact
  grammar and rules into bootstrap, authoring-tool descriptions, the scaffolded
  core/workflow skills, and the packaged plugin skills.
- Make both distribution modes independently sufficient: an MCP-only client can
  author correctly from bootstrap and tool schemas, while a plugin/skill install
  carries the same contract without relying on repository instructions or any
  private vault context.
- Update compiled-note templates and examples to default to real open-vocabulary
  `[category]` observations rather than prose-only structural headings, and add
  parity and leak gates that fail when shipped contract copies drift or private
  material enters a distributable surface.

This tightens new active-note writes and is intentionally behavior-changing for
clients that currently submit compiled notes with no semantic units. It does not
close the category vocabulary, impose a unit-count quota beyond one, rewrite
legacy pages, or change arbitrary Markdown, dataset-card, template, Source, or
Evidence creation.

## Capabilities

### New Capabilities

- `portable-agent-contract`: A canonical, versioned authoring contract projected
  into independently sufficient MCP and plugin/skill distributions with parity
  and privacy gates.

### Modified Capabilities

- `semantic-unit-language`: Rich blocks gain hierarchy-aware, non-overlapping
  parsing and empty rich units are excluded with a stable diagnostic.
- `semantic-write-contract`: The shared write boundary requires at least one
  valid semantic unit for governed active compiled pages under one exact
  applicability predicate while preserving legacy/posthoc behavior.
- `agent-bootstrap-contract`: The default compact bootstrap profile must carry
  the complete minimum semantic-authoring contract needed by a generic client.
- `command-surface`: Generated authoring-tool descriptions and schemas must expose
  the same canonical contract and compiled-note remediation across MCP, REST,
  CLI, OpenAPI, and capability documentation.

## Impact

The change affects semantic parsing and contract evaluation, compiled-note and
Tier-2 writers, bootstrap payloads, command-registry descriptions, generated
surface fixtures/docs, scaffolded page templates and workflow skills, Claude
Code plugin packaging, audit findings, and focused contract/parity/privacy tests.
It builds on `add-first-class-semantic-language`; implementation must land after
that change is available on the target branch.
