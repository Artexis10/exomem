## MODIFIED Requirements

### Requirement: A snapshot holds the vault and the non-rebuildable state, taken consistently

A backup SHALL write one restic snapshot that holds the vault root and a staged copy of every external state family that declares itself not rebuildable and does not stay out of backups. It SHALL exclude every other family. A stop request SHALL end staging between files. It SHALL NOT hold the vault mutation guard while it runs. When staging a state file fails, the backup SHALL still snapshot the vault, SHALL record the outcome `partial` with the failing code, and SHALL NOT apply retention.

#### Scenario: A live SQLite database

- **WHEN** a non-rebuildable SQLite database is being written during a backup
- **THEN** the snapshot holds a copy that SQLite opens and that passes its quick integrity check
- **AND** the snapshot holds no `-wal`, `-shm` or `-journal` file of it

#### Scenario: Rebuildable indexes stay out

- **WHEN** the state root holds embedding, lexical and graph indexes
- **THEN** the snapshot holds none of them

#### Scenario: A family that stays out of backups

- **WHEN** the state root holds the face store, which is not rebuildable and stays out of backups
- **THEN** the snapshot holds no file of it

#### Scenario: Foreground writes continue

- **WHEN** a backup runs and a client writes a note
- **THEN** the write completes without a busy refusal caused by the backup

## ADDED Requirements

### Requirement: A staged SQLite database is one consistent copy

A backup SHALL copy each staged SQLite database with SQLite's online backup API in one step, so the copy reads one snapshot of the database, through a connection that refuses statements that change data; SQLite's own recovery and checkpoint may still write the database file without changing its content. It SHALL NOT copy a write-ahead-log, shared-memory or journal file, and SHALL NOT leave one beside a database that was closed cleanly.

#### Scenario: A cleanly closed database

- **WHEN** a backup stages a SQLite database that its owner closed cleanly before staging
- **THEN** no `-wal`, `-shm` or `-journal` file of it exists beside it after staging
