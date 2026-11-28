## ADDED Requirements

### Requirement: A Kind Trait Marks The Rich Kind That Holds A Date

The closed kind trait `dated_default` SHALL mark the kind that a writer uses to keep `as_of` or
`until` when a caller names no kind. A compact observation SHALL stay metadata-free. Only the shipped
pack SHALL give the trait, to one rich kind that carries no assertion trait, and the registry SHALL ignore
it on a vault kind and report a warning finding.

#### Scenario: The shipped pack marks one kind as the dated default
- **WHEN** the shipped kinds pack is read
- **THEN** exactly one kind carries `dated_default`, that kind is `record`, and it carries no `assertion` trait

#### Scenario: A vault kind cannot carry the trait
- **WHEN** a vault registry gives `dated_default` to its own kind
- **THEN** the registry loads, the kind does not carry the trait, and the findings list a `trait_ignored` warning for it

#### Scenario: A compact observation carries no date
- **WHEN** a compact observation line is parsed
- **THEN** the resulting unit has no `as_of` and no `until`
