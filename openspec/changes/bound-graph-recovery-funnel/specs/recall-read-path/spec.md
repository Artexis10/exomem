## ADDED Requirements

### Requirement: Access-policy loading fails closed on transient errors

A transient failure to stat or read the access-policy file SHALL NOT install or cache a policy more permissive than the last successfully loaded one, and SHALL NOT change the access fingerprint used in recall identity. Transient errors reuse the last successfully loaded policy and fingerprint regardless of whether the stat signature moved - reuse never widens visibility, and convergence to the changed content happens at the next successful read; when no successful load exists, the system SHALL treat every path as excluded (fail closed) until a read succeeds. A failure in the read position after a successful stat - including the file vanishing between stat and read - is a transient error, never a policy change; only an absence observed at stat time is the genuine missing-policy identity. Recall identity SHALL only change when the policy content provably changed.

#### Scenario: A transient read error cannot widen visibility

- **WHEN** reading the access-policy file raises a transient error while a previously loaded policy exists
- **THEN** the previously loaded policy and its fingerprint remain in effect
- **AND** no cache entry is installed that outlives the error

#### Scenario: No successful load yet fails closed

- **WHEN** the access-policy file exists but has never been successfully read in this process
- **AND** reading it raises a transient error
- **THEN** access-gated serving treats all paths as excluded until a read succeeds

#### Scenario: A transient blip does not flip recall identity

- **WHEN** a stat or read blip occurs with the policy file's content unchanged
- **THEN** the recall identity and its access fingerprint are unchanged
- **AND** no recall generation advances and no admission refusal results

#### Scenario: A file vanishing between stat and read is transient

- **WHEN** the access-policy file exists at stat time and vanishes before the read completes
- **THEN** the previously loaded policy and its fingerprint remain in effect
- **AND** only an absence observed at stat time transitions to the missing-policy identity
