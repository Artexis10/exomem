## ADDED Requirements

### Requirement: Hosted frozen-verifier activation is separately resource-admitted

A Hosted cell SHALL NOT receive a frozen-verifier capability grant, verifier gate,
runtime dependency, or model artifact merely because a local repository pin is
admitted. Hosted activation SHALL require reviewed measurements of the exact
runtime and artifact on the node in service, including image size, cold and warm
latency, peak resident memory, concurrent-cell capacity, bounded scheduling or
idle reclamation, and failure isolation. The exact hosted artifact/map pair SHALL
also pass the repository's real multilingual fixture set. Until that evidence is
accepted, the Hosted image and trusted cell configuration SHALL keep the verifier
absent and off while preserving all ordinary capture, recall, and review behavior.

#### Scenario: A local pin lands without Hosted capacity evidence

- **WHEN** a release contains an admitted local verifier but no accepted Hosted
  resource envelope for its exact runtime and artifact
- **THEN** the Hosted image carries no verifier payload and trusted cell
  configuration grants no verifier capability or gate

#### Scenario: A smaller artifact fails semantic admission

- **WHEN** a compact or quantized candidate fits the cell resource envelope but
  misses any required real multilingual fixture
- **THEN** it remains unadmitted and cannot be enabled in Hosted

#### Scenario: Hosted verifier absence preserves the product

- **WHEN** a Hosted tenant runs capture, recall, and review with the verifier
  withheld
- **THEN** those surfaces retain their existing behavior and only verifier
  polarity metadata is absent
