# Teach Cross-Domain Category Examples

## Why

The portable category core shipped with teaching examples that are almost entirely
software-flavored (`#code`, `#api`, retry windows, adapters). The category system itself
is domain-neutral — fourteen of the sixteen core keys are epistemic roles, and open
vocabulary already makes `[nutrition]` or `[health]` first-class — but an agent reading
the contract today reasonably concludes Exomem is a coding tool. The benchmark
competitor teaches no vocabulary at all, so deliberate cross-domain teaching is a
differentiator we currently leave unclaimed.

## What Changes

- Swap the projected contract's role and domain examples to non-software life domains
  and add one bounded `breadth` example set (three distinct non-software domains plus
  one retained software line) so the taught contract visibly spans life, finance,
  legal/career, and code.
- Replace the rich example with a life-domain Decision exercising the same feature set
  (governed kind, stable id, tags, typed relation, governed-relative wikilink,
  category-defaults-to-kind).
- Bump the canonical semantic authoring contract version 3 → 4 and re-project it into
  every carrier: scaffold skill, nine workflow skills, public semantic-language
  reference, generated plugin skills, tool guidance, bootstrap profiles, tool-surface
  fixtures, and generated capability docs.
- Add a short hand-authored "one contract, every domain" section to the generic scaffold
  outside the projected block with a broader generic archetype example set.
- Keep everything advisory and open: no core-key changes, no ranking boost, no write
  rejection, no new registry entries.

## Dependency Note

The `portable-category-authoring` capability base still lives in the unarchived change
`teach-portable-category-core` (shipped in PR #308); `openspec/specs/` has no
consolidated base spec yet. This change's MODIFIED requirement is authored against that
change's spec text and should be reconciled when the #308 changes are archived/synced.

## Capabilities

### Modified Capabilities

- `portable-category-authoring`: The Rich Semantic Teaching Examples requirement gains
  cross-domain breadth; a new requirement pins the breadth example set and its
  projection boundaries.

## Impact

Expected implementation areas: the semantic authoring contract module and its rendered
projections, the generic scaffold and workflow skills, generated plugin artifacts, the
public semantic-language reference and capability docs, tool-surface fixtures and the
connector rollout contract, plus contract, bootstrap, teaching, and no-private-leak
tests. Existing notes, categories, and registries are untouched; this is a teaching
content change only.
