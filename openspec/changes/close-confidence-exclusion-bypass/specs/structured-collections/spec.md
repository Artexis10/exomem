# structured-collections — delta

## MODIFIED Requirements

### Requirement: Minimal typed item schemas
Collection schemas SHALL support required and optional open-vocabulary fields with bounded primitive types, enums, arrays, objects, date or datetime values, units metadata, and link fields, except that a schema SHALL NOT declare a schema-excluded frontmatter field name. The substrate SHALL impose only identity and schema-version mechanics universally; occurred time, status, units, provenance, relations, uncertainty, reconstruction, and lifecycle SHALL be present only when the collection schema makes them meaningful, and uncertainty SHALL NOT be expressed as a numeric confidence field because the schema-excluded set forbids that name.

#### Scenario: Domain fields validate before publication
- **WHEN** an item violates a required field, type, enum, identifier, or declared unit constraint
- **THEN** append or update refuses before any canonical file or history entry is published

#### Scenario: Optional schema inference is advisory
- **WHEN** Exomem infers a candidate schema from existing human-authored items
- **THEN** it returns a bounded proposal with provenance and does not make that proposal binding or rewrite historical items without explicit adoption

#### Scenario: A schema-excluded field name cannot be declared
- **WHEN** a manifest declares a schema-excluded name among its item schema fields or as its Markdown-log note field
- **THEN** collection create and revise refuse before any bytes are published, and the projected manifest authoring contract discloses the excluded names so a client authoring from the contract alone never has to guess one

#### Scenario: A schema declaring one before this contract stays parseable
- **WHEN** a manifest written before this contract declares a schema-excluded name
- **THEN** parsing, loading, discovery, resolution, and query continue to behave exactly as before, because the refusal lives at the write entry points and never in the parser
