## MODIFIED Requirements

### Requirement: Bulk upsert inherits exactly-once and withheld-as-absent
A batch SHALL be authorised over the complete collection item set and every path it would touch before any write, exactly as a single append is, and SHALL commit exactly once or leave no file, manifest change or audit entry. When any manifest, source or item path of the collection is withheld from the requesting audience the call SHALL refuse `COLLECTION_NOT_FOUND`, indistinguishable from an absent collection, before any row is planned, so no outcome, count or echo can reveal the existence or content of an item the audience may not read. The one exception is the vault owner on a connection that the host's connector boundary limits: that caller already holds the collection, so the call SHALL refuse `COLLECTION_CONNECTOR_LIMITED` with a remediation that names how to make the write (the owner's CLI or a stdio connection, or a boundary entry for this client that can write the manifest and every row). The code, message and remediation SHALL be identical whether or not any item is withheld, so the refusal reveals no withheld item.

#### Scenario: A withheld item makes the collection read as absent
- **WHEN** any item of the collection is withheld from the requesting audience
- **THEN** the call refuses `COLLECTION_NOT_FOUND`, identical to an absent collection, and nothing about the withheld item appears in the response or on disk

#### Scenario: Crash between commit and response
- **WHEN** the response is lost after the batch committed
- **THEN** a retry, with or without a transport identity, adds no second event and no duplicate item on a naturally keyed collection

#### Scenario: An owner on a connector-limited connection hears why the write is refused
- **WHEN** the vault owner writes a collection over a connection that the host's connector boundary limits
- **THEN** the call refuses `COLLECTION_CONNECTOR_LIMITED` with a remediation naming the CLI or stdio route and the boundary entry, writes nothing, and returns the same code, message and remediation whether or not any item is withheld
