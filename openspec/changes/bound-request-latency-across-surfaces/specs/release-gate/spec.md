## ADDED Requirements

### Requirement: The free-text artifact census is published, not walked per request

The census that free-text gating resolves artifact references against SHALL be published per vault
as a base and an add-only overlay. Before a service write acknowledges, the write path SHALL add
each created or renamed artifact's aliases to the overlay. The file watcher SHALL add an externally
created artifact's aliases when its event arrives. A background owner SHALL rebuild the base from
the directories that events name and from a periodic walk that bounds missed events. A request
SHALL read the base and the overlay without enumerating the filesystem. An unresolved token that has
the shape of a vault path SHALL be checked against the filesystem directly. An alias SHALL stay in
the census until a base rebuild proves that its file is gone. When no census is published, the
request SHALL take the existing synchronous walk. The release decision for each resolved artifact
SHALL stay per request.

#### Scenario: A withheld artifact written through the service is withheld at once

- **WHEN** a service write creates an artifact that the caller may not see, and the background
  owner has not run since
- **THEN** released text that names the artifact bare, by path or as a wikilink replaces the
  reference with the withheld marker

#### Scenario: An external create is withheld after its event

- **WHEN** a withheld artifact is created outside the service and the watcher delivers its event
- **THEN** released text that names the artifact replaces the reference with the withheld marker
- **AND** the request did not enumerate the filesystem

#### Scenario: A path-shaped reference to an unindexed artifact is checked directly

- **WHEN** released text names a withheld artifact by its vault path, and the census does not yet
  hold that path
- **THEN** the gate checks the path on the filesystem and replaces the reference with the withheld
  marker

#### Scenario: A removed name cannot expose a shorter withheld name

- **WHEN** a file whose name contains a shorter withheld artifact name is removed
- **THEN** the removed name stays in the census until a base rebuild proves the removal
- **AND** released text never shows the shorter withheld name because of the partial removal

#### Scenario: An unpublished census is never empty

- **WHEN** the gate runs before any census is published for the vault
- **THEN** the request takes the existing synchronous walk
- **AND** the gate never resolves references against an empty census

#### Scenario: Release decisions stay per request

- **WHEN** an artifact's release decision changes after the census was published
- **THEN** the next request applies the new decision to that artifact
