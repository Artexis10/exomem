## ADDED Requirements

### Requirement: A restore drill judges what the backup read

A restore drill SHALL require each backed-up reference entry unchanged since the snapshot's time to be restored with the same content. It SHALL report later changes and later removals as counts without failing on them. It SHALL fail on a comparison that cannot run, and SHALL end inconclusive when it judged no vault entry.

#### Scenario: The live cell moved on after the backup
- **WHEN** a drill restores a snapshot, and the serving cell has since written lock files and rewritten its indexes under the host root
- **THEN** the drill reports those entries as changed since the backup, judges the rest, and its verdict is pass

#### Scenario: The backup missed an unchanged file
- **WHEN** a vault file that the reference held unchanged since before the snapshot is missing from the restore
- **THEN** the drill's verdict is fail

#### Scenario: Nothing was old enough to judge
- **WHEN** the reference vault holds files and every one of them changed after the snapshot
- **THEN** the drill's verdict is inconclusive, not pass

#### Scenario: The comparison could not run
- **WHEN** the judging step fails to read a hash list
- **THEN** the drill's verdict is fail
