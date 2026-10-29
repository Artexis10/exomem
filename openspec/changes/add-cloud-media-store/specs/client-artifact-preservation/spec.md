## MODIFIED Requirements

### Requirement: The upload handoff advertises resumable sessions

On a server that serves upload sessions, an upload handoff SHALL advertise resumable transport for its credential-bound destination lane.
The descriptor SHALL name the creation URL, protocol version, session-secret header, request and session byte bounds, required metadata, and a documented recipe.
Evidence sessions SHALL retain their existing destination metadata and archive behavior.
Source sessions SHALL require filename and SHA-256, validate Source metadata before receiving bytes, and publish through the canonical Source writer.
Session creation SHALL refuse missing advertised metadata with `INVALID_UPLOAD`, name every missing key, and open no session.
Advertisement and creation SHALL share the active writer's format admission and byte bound.
An unsupported or unavailable write format SHALL have no usable Source resumable advertisement and SHALL refuse creation before receiving bytes.
This compatibility check SHALL NOT request authority or change its mode.
Only older records without a lane SHALL default to Evidence; an unsupported recorded lane SHALL be refused.
A caller SHALL NOT change its authorized lane through metadata, and Source sessions SHALL refuse archive expansion.

The existing mutation replay protocol SHALL identify each Source session by immutable session metadata and a stable private identity.
A lost acknowledgement SHALL NOT allocate another Source pair.
An unknown canonical outcome SHALL retain uploaded bytes under the existing expiration contract without claiming successful preservation.
Source receipts SHALL distinguish preserved bytes from unassessed semantic compilation; the active agent remains responsible for compilation.
Both resumable and multipart Source uploads SHALL return custody fields without corpus-derived warnings, structure suggestions, or vocabulary advisories.

After an uncertain part response, an uploader SHALL read the accepted offset before repeating a write.
It SHALL preserve accepted bytes and retain resumable state when connectivity prevents reconciliation.
Session creation SHALL NOT be repeated automatically after an unknown creation outcome.
An expired creation capability SHALL NOT invalidate the independent secret of a session that already exists.
Cloud gateway proxying and live acceptance remain separate rollout prerequisites.

#### Scenario: An agent with a shell uploads a file larger than the proxy edge cap

- **WHEN** an agent creates an Evidence session from its advertised handoff, sends parts, and loses the connection once
- **THEN** it continues from the offset that the server reports
- **AND** the receipt names the SHA-256 of the whole file

#### Scenario: Creation lacks a required metadata key

- **WHEN** a client creates a session without one of the advertised `Upload-Metadata` keys
- **THEN** creation refuses with `INVALID_UPLOAD`, names every missing key, and opens no session

#### Scenario: A Sources-lane handoff

- **WHEN** an agent mints a Source upload capability on a server whose active writer supports its original
- **THEN** the handoff advertises Source resumable transport with filename and SHA-256 metadata
- **AND** an unsupported or unavailable active write format omits that descriptor and reports why

#### Scenario: Raw material exceeds the single-request bound

- **WHEN** an agent selects the Source lane and uploads an original through resumable parts
- **THEN** the original and its page are stored under Sources with the declared whole-file digest
- **AND** Evidence destination metadata cannot redirect the upload
- **AND** the receipt reports semantic compilation as unassessed

#### Scenario: Source acknowledgement is lost

- **WHEN** Source publication finishes but the upload session has not saved its terminal receipt
- **THEN** the session recovers its existing mutation result without creating another Source pair
- **AND** a canonical outcome that cannot be recovered remains unknown with its uploaded bytes retained

#### Scenario: Source metadata is invalid

- **WHEN** declared Source metadata fails the canonical Source schema or destination admission
- **THEN** session creation refuses before receiving artifact bytes
- **AND** the refusal creates no partial upload

#### Scenario: A Source advisory refers to withheld content

- **WHEN** the canonical Source writer finds a related private Source during either upload transport
- **THEN** the upload receipt retains the new capture's identity, exact hash, and size
- **AND** the receipt exposes no corpus advisory, related private path, or structure count

#### Scenario: The active writer cannot accept Source originals

- **WHEN** the active write format does not support the canonical opaque Source original
- **THEN** the handoff reports Source resumable transport as unavailable without a creation URL
- **AND** direct session creation refuses before receiving bytes
- **AND** the existing canonical commit gate remains authoritative if runtime state changes later

#### Scenario: A part times out after accepting bytes

- **WHEN** a part reply times out after the service accepts bytes
- **THEN** the uploader reads the retained offset before continuing
- **AND** it reduces subsequent part size without discarding accepted bytes
