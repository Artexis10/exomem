## ADDED Requirements

### Requirement: Privacy copy discloses the media store

Where a Cloud cell uses the media store, user-facing privacy copy SHALL state that:
- originals are encrypted inside the tenant's cell before they leave it;
- the storage provider and the media edge see only ciphertext and keyed object names;
- the media bundle is wrapped under the cell root, following `cloud-cell-key-custody`;
- in standard mode, the platform can unwrap that root and the operator could deliberately decrypt stored originals.

The copy SHALL describe only the cell's enforced mode.
In standard mode, it SHALL NOT claim that stored originals are technically unreadable to the operator.
A tenant-held claim SHALL apply only after `lock_complete_at` and SHALL retain the unlocked-node attack disclosure from `cloud-operator-access`.
The storage-path ciphertext claim SHALL NOT imply that the gateway or an authorized export never handles plaintext.

#### Scenario: Privacy copy review with the media store enabled

- **WHEN** Cloud privacy copy is published or changed while any cell uses the media store
- **THEN** it names the media store, its encryption in the cell and the media bundle's custody under the cell root
- **AND** standard-mode copy states that the operator can decrypt stored originals

#### Scenario: Tenant-held custody is not yet complete

- **WHEN** a cell lacks `lock_complete_at`, that time has not passed, or tenant lock is not implemented
- **THEN** its media privacy copy makes no tenant-held or operator-unreadable claim
