## ADDED Requirements

### Requirement: The Audit Finds Entity Facet Claims Without Unit Identity

The audit SHALL report `entity_facet_unaddressed` for each top-level list item on a page whose
note-type role is `entity` when the item is outside `## Observations`, does not parse as a unit or
a markdown relation, holds text besides links, carries no Dataview inline field and fits on one
line. The check SHALL use the span target's eligibility rule. The finding SHALL name the page, the
heading and the item's line.

#### Scenario: A facet item is reported
- **WHEN** an entity page has a facet section with a prose list item
- **THEN** the audit reports `entity_facet_unaddressed` with the page, heading and line

#### Scenario: Link-only items and units are not reported
- **WHEN** a list item holds only wikilinks, already parses as a unit or a markdown relation, or
  carries a Dataview inline field
- **THEN** the audit does not report it

### Requirement: The Facet Fix Converts Items Through A Previewed Plan

`audit_fix` SHALL offer the conversion of these items as a previewed plan. The plan SHALL convert
each item in place to a compact unit whose category is the canonical category of its nearest `##`
heading, keep the item's text bytes, and add a generated anchor. The conversion SHALL run only as a
maintenance job that names the previewed plan id, in guarded chunks, and SHALL change no other
line. A local apply SHALL still write the page's other fixes.

#### Scenario: The previewed fix converts items in place
- **WHEN** the owner applies the previewed plan
- **THEN** each reported item becomes a compact unit with its text unchanged and an anchor
- **AND** a later supersede can name that anchor

#### Scenario: A preview writes nothing
- **WHEN** `audit_fix` runs as a dry run
- **THEN** it returns the plan id and every item the plan covers, each facet item among them, and
  no page changes

#### Scenario: A local apply keeps the page's other fixes
- **WHEN** an entity page holds a facet item and a non-canonical wikilink, and `audit_fix` applies
  locally
- **THEN** the wikilink becomes canonical and the facet item is unchanged
