## MODIFIED Requirements

### Requirement: Report the observed software and recall identities honestly

Discovery SHALL identify the package version and deterministic package-file digest observed during server composition.
The response SHALL state that observation's scope and MUST NOT claim to attest model weights or mutable configuration.
The recall space SHALL come from the currently published embedding identity without model loading.
Its public identity SHALL be an opaque digest and SHALL equal the `recall_space_id` returned by passage search.
Unavailable software or recall identity MUST remain explicitly unknown or unavailable.
Discovery SHALL report the recall space as `unavailable` only when the store holds no published identity.
When discovery cannot read the store, it SHALL report the recall space as `unknown` with a null id, and MUST NOT omit the field or report it as `unavailable`.
Discovery SHALL wait a bounded time for a busy store before it reports `unknown`.

#### Scenario: A source overlay has the same package version

- **WHEN** the package files differ while their version string remains the same
- **THEN** their composed package-file digests differ
- **AND** discovery does not equate the two implementations from their version alone

#### Scenario: No recall space has been published

- **WHEN** the store holds no published recall-space identity
- **THEN** discovery returns an explicit unavailable recall state
- **AND** it does not initialize an encoder to fill that field

#### Scenario: The store stays busy past the wait

- **WHEN** another reader or a capture holds the store for longer than discovery waits
- **THEN** discovery returns an unknown recall state with a null id
- **AND** it does not report the published space as unavailable

#### Scenario: Concurrent discovery calls

- **WHEN** several discovery calls arrive together on a cell with a published recall space
- **THEN** each call reports that space as observed
