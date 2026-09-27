## ADDED Requirements

### Requirement: A lexical catalogue schema change does not turn an upgrade into a cold start

When the serving worker's live lexical catalogue carries a schema or semantic catalogue identity other than the target release's, the standby SHALL build the target release's complete catalogue into a lexical rebuild temporary that it holds under the temporary's advisory lock for its whole life. The build SHALL take no publication barrier and make no source proof, SHALL leave the live catalogue and every other file the serving worker owns byte-for-byte unchanged, and SHALL change neither the catalogue path nor any reserved-path descriptor. The standby's `lexical` cutover component SHALL be ready once that catalogue is built. A live catalogue at the target's schema and identity that merely lags the corpus, or one that is missing, SHALL remain the serving worker's repair owner's to catch up, and the standby's `lexical` component SHALL keep waiting as before.

Promotion SHALL adopt the standby-built catalogue only after acquiring ownership and only while it still carries the target schema and the current semantic identity, replacing the live set exactly as the background repair replaces it, and the handoff record SHALL report `lexical_catalogue` as `adopted` or `rebuild-after-promotion`. An adopted catalogue SHALL be carried to the promoted worker's warm, which SHALL reconcile it once, after the watcher's seed and before any repair, re-parsing only the pages changed since the build, and then prove and admit it. Writes that landed between the build and promotion SHALL NOT cost a whole-catalogue rebuild; a replacement that preserved a file's modification time remains covered by the existing rebuild rung. A standby stopped without promotion SHALL remove its temporary catalogue, and no temporary reaper SHALL remove one while its builder still holds it.

A worker that starts without a standby, as every hosted cell does, SHALL keep rebuilding a schema-changed catalogue on its own state volume through the ordinary repair at boot and admit retrieval once that catalogue is current.

A standby that is discarded without reaching cutover readiness SHALL NOT be followed by a cold replacement unless the upgrade request explicitly allows one in its own control field, separate from the target. Otherwise the upgrade SHALL report failure before ingress is paused, and the serving worker SHALL keep serving without being drained, stopped or signalled. A roll-forward of a recorded transition is exempt, because its worker has already stopped. The worker protocol version SHALL NOT change for this.

#### Scenario: A schema-changed release cuts over through the standby's catalogue
- **WHEN** a managed upgrade targets a release whose lexical catalogue schema differs from the one the serving worker keeps current
- **THEN** the standby reaches cutover readiness while every pre-existing byte of the vault and of the vault's state directory is unchanged and the only new files are its rebuild temporary
- **AND** promotion adopts that catalogue and records `lexical_catalogue: adopted`
- **AND** writes the serving worker made after the build are searchable once retrieval is admitted, with no whole-catalogue rebuild

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

#### Scenario: A discarded standby refuses the upgrade by default
- **WHEN** the standby is discarded because it did not reach cutover readiness and the request carries no cold-replacement override
- **THEN** the upgrade reports failure with the handoff record naming the waiting component, before ingress is paused
- **AND** the serving worker keeps serving and no transition is recorded

#### Scenario: The operator allows a cold replacement
- **WHEN** the upgrade request carries the explicit cold-replacement override and the standby is discarded
- **THEN** the upgrade continues through the one-worker sequence and the handoff record says the cold replacement was allowed
