## ADDED Requirements

### Requirement: Shared-workload improvement is repeatable and work-bounded

Claims of shared Markdown workflow parity SHALL use at least three fresh paired
runs per declared corpus size with identical fixed bodies, public operations,
runtime pins, and isolated state. Product order SHALL alternate. The report SHALL
retain individual observations and compare median verified-closure times;
startup and optional graph convergence SHALL remain separate measurements.
Deterministic regression checks SHALL also measure unrelated body reads during
warm writer resolver preparation and topology dependency discovery. A timing
improvement SHALL NOT substitute for full-rebuild parity or durability evidence.

#### Scenario: One unusually fast run
- **WHEN** one observation meets the target but the paired median does not
- **THEN** the report records the observations and does not claim the parity target was met

#### Scenario: Background graph completion is delayed
- **WHEN** exact public reads and text search verify closure before graph convergence
- **THEN** the report names the two states separately and does not treat useful closure as proof that all derived work finished

#### Scenario: Work-count regression despite fast hardware
- **WHEN** a warm resolver or dependency-discovery path rereads unrelated Markdown
- **THEN** its deterministic regression check fails even if elapsed time remains under a wall-clock threshold
