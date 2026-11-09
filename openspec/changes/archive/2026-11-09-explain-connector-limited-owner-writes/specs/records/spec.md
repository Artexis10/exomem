## MODIFIED Requirements

### Requirement: Bulk upsert inherits exactly-once and withheld-as-absent
A batch SHALL be authorised over the complete collection item set and every path it would touch before any write, exactly as a single append is, and SHALL commit exactly once or leave no file, manifest change or audit entry. When any manifest, source or item path of the collection is withheld from the requesting audience the call SHALL refuse `COLLECTION_NOT_FOUND`, indistinguishable from an absent collection, before any row is planned, so no outcome, count or echo can reveal the existence or content of an item the audience may not read. The one exception is the vault owner on a connection that the host's connector boundary limits, when the manifest is readable on this connection and only the boundary's write ceiling refuses the write: the call SHALL refuse `COLLECTION_CONNECTOR_LIMITED` with a remediation that names where the boundary does not limit the write (the owner's CLI or a stdio connection, or a client whose boundary entry denies it no scope). That owner SHALL get the same `COLLECTION_NOT_FOUND` as an absent collection when its write is refused for any other reason, including a manifest hidden from this connection. The code, message and remediation of the exception SHALL depend only on the manifest, so they are identical whether or not any item is withheld and reveal no withheld item.

#### Scenario: A withheld item makes the collection read as absent
- **WHEN** any item of the collection is withheld from a requesting audience that the connector-limited owner exception does not cover
- **THEN** the call refuses `COLLECTION_NOT_FOUND`, identical to an absent collection, and nothing about the withheld item appears in the response or on disk

#### Scenario: Crash between commit and response
- **WHEN** the response is lost after the batch committed
- **THEN** a retry, with or without a transport identity, adds no second event and no duplicate item on a naturally keyed collection

#### Scenario: An owner on a connector-limited connection hears why the write is refused
- **WHEN** the vault owner writes a collection whose manifest its connection reads, and only the host's connector boundary's write ceiling refuses that write
- **THEN** the call refuses `COLLECTION_CONNECTOR_LIMITED` with a remediation naming the CLI or stdio route and a client whose boundary entry denies it no scope, writes nothing, and returns the same code, message and remediation whether or not any item is withheld

#### Scenario: A limited owner cannot tell a hidden collection from an absent one
- **WHEN** the vault owner on a connector-limited connection writes a collection whose manifest that connection may not read, or whose write a rule other than the boundary's write ceiling refuses
- **THEN** the call refuses `COLLECTION_NOT_FOUND` with the same code, message and remediation as a write to an absent collection, and writes nothing
