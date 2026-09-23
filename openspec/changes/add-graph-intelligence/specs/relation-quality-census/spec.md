## ADDED Requirements

### Requirement: Counts-only relation census over one published snapshot
The system SHALL provide a relation-quality census computed from one published graph
snapshot and the relation and entity-type registries. The census MUST NOT parse
Markdown, run a model, build an index or write. It SHALL report integers and ratios
derived from them under fixed definitions: authored edges by status (`core_specific`,
`core_generic`, `extension`, `alias`, `deprecated`, `unregistered`, `scope_violation`),
generic share, typed and specific coverage, predicate utilisation, extension use,
disconnected and isolated pages, entity coverage, unregistered pressure and inverse
duplicates, over a cohort of eligible pages that Sources and Evidence never join. The
indexer's unit-to-own-page structural edges SHALL NOT count as relations or connections.
Metrics that depend on vocabulary standing or on an agent's judgement SHALL be reported
as `unmeasured` until their inputs exist.

#### Scenario: One snapshot, no Markdown
- **WHEN** the census runs on a vault with a current graph
- **THEN** it opens exactly one read snapshot and no page parser is invoked

#### Scenario: Coverage and generic share follow the definitions
- **WHEN** a fixture holds known specific, generic, alias, deprecated, unregistered and out-of-scope authored edges, a page typed only by frontmatter sources, and a page linked only by wikilinks
- **THEN** each status count, the generic share over registered authored edges, and the typed and specific coverage match the fixture exactly

#### Scenario: Disconnected pages count every origin
- **WHEN** eligible pages are connected only by typed relations, only by wikilinks, only by frontmatter sources, or not at all
- **THEN** only the pages with no outbound edge to another page count as disconnected, and those with no inbound edge either count as isolated

#### Scenario: Entity coverage names generic-only entities
- **WHEN** a person entity is linked to an organization only by `relates_to` and an organization has a specific edge to another organization
- **THEN** the census counts one entity with a specific entity edge, one entity whose entity edges are all generic, one person without an affiliation-family edge, and the entities with no inbound edge

### Requirement: Structural false-precision checks report their denominators
The census SHALL run structure-only checks over the typed edges authored on eligible
pages: `signature_mismatch`, `supersedes_backwards`, `evidence_target_not_evidential`,
`answers_without_question`, `same_page_epistemic`, `directed_both_ways` and
`contradicts_within_chain`. Each check SHALL report the edges it can apply to and the
violations among them, so a check that cannot fire is not counted as a pass. An edge with
a placeholder endpoint SHALL NOT count as applicable to a rule that inspects endpoints.
`directed_both_ways` SHALL be marked as inspection-only.

#### Scenario: Each check fires once on a planted violation
- **WHEN** a fixture plants one violation for each check beside edges that pass it and edges it cannot apply to
- **THEN** each check reports exactly its applicable edges and one violation

### Requirement: The census follows the caller's release filter
The census SHALL admit a node only when its page passes the caller's release filter and
structural exclusion, and an edge only when both endpoints and the page that authored it
are admitted, filtering inside the walk. No withheld page SHALL change any count, ratio or
check. The graph generation SHALL be reported only to a caller without a release
restriction. In counts mode the output SHALL name no path, title, label or vault
extension key; `detail="keys"` MAY add predicate keys and per-predicate counts.

#### Scenario: Withheld material is invisible
- **WHEN** two vaults differ only in a withheld page, its authored edges, and edges from visible pages to it
- **THEN** a caller for whom that page is withheld receives byte-identical census output from both, in counts and keys detail

#### Scenario: Counts mode carries no identifiers
- **WHEN** the census runs in counts mode on a vault with extension keys, unregistered labels and distinctive titles
- **THEN** the serialized output contains none of them, while keys mode names the extension keys

### Requirement: The census is reproducible and honest about availability
For one graph generation and registry hash the census output SHALL be byte-identical
across runs. It SHALL report `census_version`, `exomem_version`, the registry core
version and extension hash, the cohort, metrics and checks. When the snapshot is
unavailable it SHALL report `available: false` with a reason and no metrics, never zero.

#### Scenario: Repeated runs are identical
- **WHEN** the census runs twice on one generation and registry
- **THEN** the two outputs are byte-identical

#### Scenario: Unavailable is not zero
- **WHEN** the graph is disabled or has never been built
- **THEN** the census reports unavailable and carries no cohort, metrics or checks

### Requirement: Census surfaces
The census SHALL be reachable as `schema_memory(subject="relations", operation="census")`
accepting only `detail` and optional `date_from`/`date_to`, as the read-only CLI
`exomem relations census`, and as one doctor line. The CLI SHALL ask a running managed
service over REST first and otherwise open the graph sidecar read-only; it MUST NOT run
out-of-process index work. The doctor line SHALL pass when the census is available and
warn otherwise.

#### Scenario: The operation refuses unrelated arguments
- **WHEN** the census operation is called with a save flag, a proposal or a hash
- **THEN** it is refused with an argument error and nothing is read or written

#### Scenario: The CLI prefers the live service
- **WHEN** a managed service answers the REST census call
- **THEN** the CLI prints its result and does not open the local sidecar
- **AND** when no service answers, the CLI reads the local snapshot under the local release filter

### Requirement: Optional agent-judged sample
The CLI SHALL write, on request, a seeded sample of specific authored edges stratified by
relation family, as refs only (page path and anchor at each end), to a local file. A
judged copy SHALL fold into `false_precision_judged` as counts per verdict (`precise`,
`too_specific`, `wrong_direction`, `wrong_predicate`, `should_be_generic`) with a 95%
Wilson interval on the false share. An unknown verdict SHALL be refused, and a sample
without verdicts SHALL leave the metric `unmeasured`. Only the folded counts re-enter the
census.

#### Scenario: Sampling is seeded and stratified
- **WHEN** the same snapshot is sampled twice with one seed
- **THEN** the samples are identical, every family with specific edges is represented, and no item carries text

#### Scenario: Judgements fold with an interval
- **WHEN** ten items are judged with three false verdicts
- **THEN** the census reports a 0.3 false share with its Wilson interval and the per-verdict counts

### Requirement: Infer reads its census from the snapshot
Relation inference SHALL read its aggregate census from the current graph snapshot,
under the caller's release filter, with the keys `relation_counts`, `page_counts` and
`denominators` unchanged. When the snapshot is unavailable, or the call is scoped to a
project, inference SHALL keep its Markdown count and remain non-authoring.

#### Scenario: Keys and values survive delegation
- **WHEN** inference runs on a vault with resolved relation targets, with and without a current graph
- **THEN** both censuses have identical keys and values, and the graph-backed one does not use the Markdown count
