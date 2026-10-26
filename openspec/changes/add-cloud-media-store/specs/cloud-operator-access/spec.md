## ADDED Requirements

### Requirement: Privacy copy discloses the media store

Where a Cloud cell uses the media store, user-facing privacy copy SHALL state that:
- originals are encrypted inside the tenant's cell before they leave it;
- the storage provider and the media edge see only ciphertext and keyed object names;
- the operator holds the cell's media key, under the same master key as the backup key, and could deliberately decrypt stored originals.

The copy SHALL NOT claim that the storage provider or the operator is technically unable to read stored originals while the operator holds that key.

#### Scenario: Privacy copy review with the media store enabled

- **WHEN** Cloud privacy copy is published or changed while any cell uses the media store
- **THEN** it names the media store, its encryption in the cell and the operator's custody of the media key
- **AND** it contains no claim that stored originals are unreadable to the operator
