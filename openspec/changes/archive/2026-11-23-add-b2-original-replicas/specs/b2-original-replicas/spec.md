## ADDED Requirements

### Requirement: B2 replicas preserve immutable encrypted versions

A declared B2 replica store SHALL use one explicitly configured bucket and prefix. The worker SHALL confirm the scope and available capabilities before a copy. It SHALL record the exact provider file ID only after establishing the size and digest. The adapter SHALL use read and write capability without a delete capability.

#### Scenario: A part or finish reply is interrupted

- **WHEN** a transient refusal interrupts a multipart upload
- **THEN** a part retry uses the same part number
- **AND** a lost finish reply yields a replica only after exact confirmation of the completed version

#### Scenario: The credential reaches a different bucket or prefix

- **WHEN** the provider returns a scope different from the declared scope
- **THEN** the copy reports the store unavailable before it opens its source

### Requirement: Verification distinguishes presence from content

The adapter SHALL use the exact provider version for metadata and byte reads. Its metadata result SHALL report the declared ciphertext digest for presence verification. Content verification SHALL read the full ciphertext and authenticate the original. An ambiguous absence or different returned identity SHALL report store unavailability.

#### Scenario: A newer provider version uses the same name

- **WHEN** the location has a newer version and the recorded version remains available
- **THEN** the worker reads the recorded version and verifies its bytes

#### Scenario: The provider refuses a specific byte range

- **WHEN** the provider gives the full file or a different range for a proper subrange
- **THEN** the adapter refuses that response before it yields bytes
