## ADDED Requirements

### Requirement: Records presentation remains neutral observed state

Managed presentation SHALL display only selected canonical observed fields. It SHALL preserve nulls, inequalities, units, ranges, cancellation/status, precision, qualifiers, and provenance without inventing bounds, certainty, diagnoses, interpretation, ranking, or domain meaning. Unrenderable values SHALL refuse rather than coerce arbitrarily.

#### Scenario: Laboratory panel is readable without interpretation
- **WHEN** panel declares summary, measurements table, notes, and collapsed provenance
- **THEN** body shows exact observations with no diagnosis, judgment, reconstructed range, or advice

#### Scenario: Source qualifiers survive rendering
- **WHEN** children include less-than, cancellation, null, or specimen qualifier
- **THEN** table preserves each distinction

### Requirement: Records queries safely project and expand one child field

Before filtering, sorting, aggregation, pagination, rendering, or returning any query, every child array named by `record_presentation` SHALL be replaced in the governed snapshot with its type-valid declared columns and audience-specific link projection. Undeclared or withheld nested values SHALL never enter expanded or unexpanded query machinery. Type mismatch SHALL refuse rather than fall back to raw objects.

`expand_child` SHALL name a table field in `record_presentation`. Each child becomes one bounded row containing parent values except selected container; parent identity/system fields; `parent_record_id`, `child_field`, `child_index`; and only the safe child projection. A hard total child cap SHALL precede materialization.

`expand_children: true` SHALL resolve only one Markdown-log container or exactly one Records Markdown-item table. Open object arrays and datasets are ineligible. No/multiple eligible fields or both selectors SHALL refuse actionably. False/omitted expansion SHALL preserve parent rows containing only the safe nested projection under the response cap.

#### Scenario: Markdown-item measurements expand instead of disappearing
- **WHEN** seven panels have `measurements` and caller uses `expand_child: measurements`
- **THEN** exact child columns paginate with parent correlation/snapshot rather than return zero

#### Scenario: Boolean resolves one table
- **WHEN** Records presentation has exactly one table and client sends `expand_children: true`
- **THEN** it behaves exactly like explicit selection

#### Scenario: Multiple tables require selection
- **WHEN** two tables exist and only boolean true is sent
- **THEN** query refuses with releasable selectors and no partial rows

#### Scenario: Child pagination avoids parent-array overflow
- **WHEN** parent array exceeds non-expanded cap but child rows fit
- **THEN** expansion omits the container and provides bounded continuation pages without duplicate/skip

#### Scenario: Unauthorized parent produces no child facts
- **WHEN** governance withholds a parent
- **THEN** expansion does not parse, count, render, or return its children

#### Scenario: Expanded and unexpanded egress match
- **WHEN** child objects have extra keys or a declared link resolves to a withheld target
- **THEN** both query modes exclude undeclared/withheld nested values before any observable query operation

#### Scenario: Policy changes query but not file
- **WHEN** link policy changes with identical canonical bytes
- **THEN** query follows current authorization while managed bytes/hash remain unchanged
