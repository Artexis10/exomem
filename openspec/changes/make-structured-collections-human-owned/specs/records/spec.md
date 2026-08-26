## ADDED Requirements

### Requirement: Records presentation recipes have one active owner

A Records Markdown-item manifest SHALL declare at most one of legacy `record_presentation` and shared `item_presentation`. Existing legacy recipes SHALL retain their current rendering and safe child-projection semantics. Conversion SHALL require a complete guarded manifest revision and a transactional replacement of every owned block; implicit dual rendering is forbidden.

#### Scenario: Existing Records manifest remains compatible

- **WHEN** an existing collection declares only a valid `record_presentation`
- **THEN** validation, mutation, query child projection, and managed rendering continue under the existing contract

#### Scenario: Dual recipes refuse

- **WHEN** a Records manifest declares both presentation recipe forms
- **THEN** validation refuses before reading or writing item content

#### Scenario: Conversion is explicit

- **WHEN** a guarded revision replaces `record_presentation` with `item_presentation`
- **THEN** the revision preview identifies every affected block and publication leaves exactly one current managed block per applicable item

### Requirement: Human-owned Records representation remains neutral observation

Records `item_filename` and `item_presentation` SHALL preserve exact observed values, nulls, inequalities, units, ranges, cancellation or status, precision, qualifiers, and provenance. They SHALL NOT add interpretation, diagnosis, ranking, reconstructed bounds, confidence, or advice. A field that cannot be represented without semantic coercion SHALL refuse rendering.

#### Scenario: Event filename carries identity but not mutable status

- **WHEN** a collection's natural key includes occurrence date, event title, and immutable event kind
- **THEN** its filename may render those values but excludes usability, approval, processing, and other mutable state

#### Scenario: Readable body preserves qualifiers

- **WHEN** selected Record values contain null, less-than, cancellation, unit, or source qualifiers
- **THEN** the managed body preserves each distinction exactly and adds no explanation of what it means

### Requirement: Records inspection reports legacy and shared representation debt

Records inspection SHALL apply the shared representation diagnostics to both presentation forms and SHALL continue to find owned legacy markers after the recipe is removed from the current manifest. The absence of a body recipe SHALL NOT make a stale or orphan block healthy.

#### Scenario: Removed legacy recipe remains inspectable

- **WHEN** a collection contains a legacy managed block but its current manifest declares no presentation recipe
- **THEN** Records inspection reports the orphan and identifies explicit cleanup or manifest restoration as remediation

