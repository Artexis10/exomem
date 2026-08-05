## ADDED Requirements

### Requirement: Missing provenance is an optional deterministic review category
The `missing_sources` audit category SHALL surface active, writable compiled pages of the
types whose frontmatter specification marks provenance required — `research-note`,
`insight`, `failure`, and `pattern` — whose `sources:` frontmatter is absent or empty. It
SHALL reuse the relation-debt exclusion set, excluding append-only, read-only, archived,
superseded, draft, index, hub, and snapshot material. Each finding SHALL be informational,
SHALL carry a content-derived signal version, and SHALL propose review rather than
mutation. The category SHALL be optional and absent from the default attention set, and
SHALL NOT be added to the per-type required-frontmatter table, so that no automatic
backfill can ever infer or fabricate provenance.

#### Scenario: Provenance-free research note is surfaced on request
- **WHEN** an active writable research note carries `sources: []` and `missing_sources` is requested explicitly
- **THEN** one informational finding is emitted for that page
- **AND** citing an existing source removes the finding on the next audit

#### Scenario: Category is opt-in and never auto-repaired
- **WHEN** attention is computed with no `categories` filter
- **THEN** `missing_sources` findings are absent from the default queue
- **AND** no audit repair pass writes, infers, or fabricates a `sources:` value

#### Scenario: Types without a provenance requirement are not flagged
- **WHEN** an experiment or production log carries empty `sources:`
- **THEN** no `missing_sources` finding is emitted for it

### Requirement: Relation review decisions are observable
Audit SHALL report a disposition-kind census computed over every evaluated page, not only
pages that produced a finding, covering typed-relation, connectivity, reviewed-none,
bootstrap, missing, and stale outcomes. An optional `relation_review_debt` category SHALL
emit one informational finding per page currently satisfied by a reviewed-none decision,
carrying that decision's recorded reason. Reviewed-none validation SHALL remain free-form
and SHALL NOT gain additional caller round trips, so that cold-start writes stay cheap.

#### Scenario: Census counts satisfied pages
- **WHEN** a batch is evaluated in which most pages are satisfied and emit no finding
- **THEN** the disposition census still counts every evaluated page by kind
- **AND** the reported reviewed-none share reflects the true rate rather than the finding-producing subset

#### Scenario: Reviewed-none reasons are surfaced for review
- **WHEN** `relation_review_debt` is requested over a vault containing reviewed-none decisions
- **THEN** each such page emits one informational finding carrying its recorded reason
- **AND** requesting the category does not alter, revalidate, or expire any decision

#### Scenario: Connectivity satisfaction is distinguishable from a typed edge
- **WHEN** the census is computed over pages satisfied by wikilinks and pages satisfied by typed relations
- **THEN** the two are reported as separate counts
