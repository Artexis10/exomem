## Why

The audit currently reports every unresolved note wikilink as broken, even when the author deliberately references a page they intend to create. That turns a frictionless authoring pattern into an error without changing the harder rule that unresolved targets cannot satisfy governed relation connectivity.

## What Changes

- Classify unresolved Markdown-page wikilinks as informational forward references rather than broken links.
- Keep definite attachment mistakes and ambiguous note targets classified as broken wikilinks.
- Clear a forward-reference finding automatically when the referenced page becomes resolvable.
- Leave semantic relation disposition unchanged: a forward reference does not satisfy connectivity.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `attention-queue`: Distinguish deliberate forward references from genuinely broken wikilinks in audit reporting.

## Impact

The audit category registry, wikilink audit classification, and focused audit tests change. The semantic write contract and relation-disposition implementation do not change.
