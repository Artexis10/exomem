## ADDED Requirements

### Requirement: A lexical catalogue schema change does not turn an upgrade into a cold start

When the serving worker's live lexical catalogue carries a schema or semantic catalogue identity other than the target release's, the standby SHALL build the target release's complete catalogue into a lexical rebuild temporary that it holds under the temporary's advisory lock for its whole life. The build SHALL take no publication barrier and make no source proof, SHALL leave the live catalogue and every other file the serving worker owns byte-for-byte unchanged, and SHALL change neither the catalogue path nor any reserved-path descriptor. The standby's `lexical` cutover component SHALL be ready once that catalogue is built. A live catalogue at the target's schema and identity that merely lags the corpus, or one that is missing, SHALL remain the serving worker's repair owner's to catch up, and the standby's `lexical` component SHALL keep waiting as before.

Promotion SHALL adopt the standby-built catalogue only after acquiring ownership and only while it still carries the target schema and the current semantic identity, replacing the live set exactly as the background repair replaces it, and the handoff record SHALL report `lexical_catalogue` as `adopted` or `rebuild-after-promotion`. An adopted catalogue SHALL be carried to the promoted worker's warm, which SHALL reconcile it once, after the watcher's seed and before any whole-catalogue repair, re-parsing exactly the pages whose file signature (modification time, change time and size) or scope membership differs from what the build parsed, removing pages that are gone, and then prove and admit it. Writes that landed between the build and promotion SHALL NOT cost a whole-catalogue rebuild, a page replaced under its old modification time SHALL NOT be admitted stale, and a reconcile that fails SHALL leave the ordinary proof and repair. A standby stopped without promotion SHALL start no new build and SHALL cancel a build still running, which stops at its next walked file or parsed page and removes its temporary catalogue; a temporary catalogue whose builder died with its process SHALL be left to the orphan sweep, and no temporary reaper SHALL remove one while its builder still holds it.

A worker that starts without a standby, as every hosted cell does, SHALL keep rebuilding a schema-changed catalogue on its own state volume through the ordinary repair at boot and admit retrieval once that catalogue is current.

#### Scenario: A schema-changed release cuts over through the standby's catalogue
- **WHEN** a managed upgrade targets a release whose lexical catalogue schema differs from the one the serving worker keeps current
- **THEN** the standby reaches cutover readiness while every pre-existing byte of the vault and of the vault's state directory is unchanged and the only new files are its rebuild temporary
- **AND** promotion adopts that catalogue and records `lexical_catalogue: adopted`
- **AND** writes the serving worker made after the build, including a page replaced under its old modification time, are searchable once retrieval is admitted, with no whole-catalogue rebuild

#### Scenario: A same-schema stale catalogue still waits
- **WHEN** the live catalogue carries the target's schema and identity but lags the corpus
- **THEN** the standby builds no catalogue and its `lexical` component stays waiting for the serving worker's repair

#### Scenario: A cold start upgrades the catalogue in place
- **WHEN** a worker starts with no standby over a catalogue an earlier release published
- **THEN** its ordinary repair rebuilds the catalogue on the same state volume and admits retrieval
- **AND** no standby handoff reconcile runs

#### Scenario: A discarded standby removes what it built
- **WHEN** a standby that built a catalogue is stopped without promotion
- **THEN** its temporary catalogue is removed and the live catalogue is unchanged
