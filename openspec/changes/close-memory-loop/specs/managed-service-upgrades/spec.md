## ADDED Requirements

### Requirement: A standby prepares a replacement lexical catalogue without publishing live state

When a read-only assessment proves incompatibility or compatible staleness, the standby SHALL prepare a complete replacement. Compatible staleness SHALL require target schema, semantic identity and well-formed stored checkpoints that differ from captured current projections. The assessment SHALL read schema, semantic identity and stored checkpoints from one SQLite snapshot. It SHALL compare stored checkpoints against explicitly captured candidate projections and revalidate the candidate's semantic identity, access identity and projections. Missing, malformed, unreadable, busy or unsampled observations SHALL NOT establish compatible staleness. The standby SHALL reconsider preparation after a transient observation, even when the serving worker publishes nothing.

A missing live catalogue SHALL retain the existing serving-owner repair behavior.

The standby SHALL hold its rebuild temporary under the temporary's advisory lock for its whole life. The build SHALL take no publication barrier and make no source proof. Before promotion, the standby SHALL NOT mutate live catalogue rows, schema, committed WAL frames, source files or owner publication state. It SHALL change neither the live catalogue path nor any reserved-path descriptor. The assessment SHALL open the existing catalogue read-only and use one SQLite read transaction. SQLite-native coordination MAY initialize and update only the live catalogue's exact SHM path and its locks.

This allowance SHALL NOT permit application writes, checkpoints, catalogue replacement or owner publication. Catalogue construction SHALL write only its held rebuild temporary. The standby's `lexical` cutover component SHALL be ready once that complete catalogue is built. This readiness SHALL express preparation only, never runtime retrieval admission. The complete held build SHALL survive ordinary source edits without another pre-cutover current-source proof.

Promotion SHALL adopt the standby-built catalogue only after acquiring ownership and only while it still carries the target schema and the current semantic identity, replacing the live set exactly as the background repair replaces it, and the handoff record SHALL report `lexical_catalogue` as `adopted` or `rebuild-after-promotion`. An adopted catalogue SHALL be carried to the promoted worker's warm, which SHALL reconcile it once, after the watcher's seed and before any whole-catalogue repair, re-parsing exactly the pages whose file signature (modification time, change time and size) or scope membership differs from what the build parsed, removing pages that are gone, and then prove and admit it. Writes that landed between the build and promotion SHALL NOT cost a whole-catalogue rebuild, a page replaced under its old modification time SHALL NOT be admitted stale, and a reconcile that fails SHALL leave the ordinary proof and repair. A standby stopped without promotion SHALL start no new build and SHALL cancel a build still running, which stops at its next walked file or parsed page and removes its temporary catalogue; a temporary catalogue whose builder died with its process SHALL be left to the orphan sweep, and no temporary reaper SHALL remove one while its builder still holds it.

A worker that starts without a standby, as every hosted cell does, SHALL keep rebuilding a schema-changed catalogue on its own state volume through the ordinary repair at boot and admit retrieval once that catalogue is current.

Final runtime admission SHALL retain the existing strict source and access checks after ownership, watcher seed and healing. Preparation alone SHALL NOT imply runtime admission or a bound on the unavailable interval.

#### Scenario: A schema-changed release cuts over through the standby's catalogue
- **WHEN** a managed upgrade targets a release whose lexical catalogue schema differs from the one the serving worker keeps current
- **THEN** the standby reaches cutover readiness without writing live catalogue content, source files, descriptors or owner publication state
- **AND** its only catalogue construction writes are its held rebuild temporary, while SQLite-native coordination may update the exact live SHM path
- **AND** promotion adopts that catalogue and records `lexical_catalogue: adopted`
- **AND** writes the serving worker made after the build, including a page replaced under its old modification time, are searchable once retrieval is admitted, with no whole-catalogue rebuild

#### Scenario: A compatible stale catalogue prepares without the serving owner's repair
- **WHEN** the assessment proves the target schema and semantic identity with well-formed stored checkpoints that differ from captured current projections
- **THEN** the standby builds a complete held temporary without writing live catalogue content or admitting retrieval
- **AND** ordinary source edits during preparation do not discard that complete build
- **AND** promotion adopts it after ownership and heals changed, new, deleted and excluded material before runtime admission

#### Scenario: A transient observation recovers without a live publication
- **WHEN** the initial assessment cannot read the live catalogue and a later assessment proves compatible staleness
- **THEN** the standby reconsiders detached preparation within the existing reproof cadence
- **AND** no publication by the serving worker is required

#### Scenario: An unknown observation does not authorize preparation
- **WHEN** the catalogue or either projection is missing, unreadable, busy or malformed
- **THEN** the observation does not establish compatible staleness and the standby publishes nothing

#### Scenario: A cold start upgrades the catalogue in place
- **WHEN** a worker starts with no standby over a catalogue an earlier release published
- **THEN** its ordinary repair rebuilds the catalogue on the same state volume and admits retrieval
- **AND** no standby handoff reconcile runs

#### Scenario: A discarded standby removes what it built
- **WHEN** a standby that built a catalogue is stopped without promotion
- **THEN** its temporary catalogue is removed without writing live catalogue content
