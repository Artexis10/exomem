## ADDED Requirements

### Requirement: Legacy Windows Runtime DACL Failures Are Actionable

Pre-existing Windows idempotency runtime state SHALL remain fail-closed when its DACL does
not satisfy the current private-runtime contract. The failure SHALL name the exact offending
path and SHALL provide an exact operator-runnable remediation command. Exomem SHALL NOT
implicitly elevate or rewrite an existing runtime DACL.

The same validation fault SHALL be visible to `exomem doctor`; an unreadable or unsafe
idempotency store SHALL NOT be reported as healthy.

#### Scenario: Pre-existing unsafe directory is rejected with a remedy

- **WHEN** an upgraded install opens a pre-existing idempotency runtime directory whose DACL
  is not protected or whose ACEs do not match the current principal-private trustee set
- **THEN** Exomem refuses to open SQLite or deserialize persisted receipt state
- **AND** the error names the exact directory path
- **AND** the error contains an exact `icacls` command that grants only the required trustees
- **AND** the error is one operator-readable line rather than a bare pathless `RuntimeError`

#### Scenario: Doctor surfaces the unsafe runtime

- **WHEN** `exomem doctor` inspects an idempotency runtime with an invalid Windows DACL
- **THEN** its idempotency-store check is not reported as passing
- **AND** the check includes the exact offending path and remediation command

#### Scenario: Existing DACL is not repaired implicitly

- **WHEN** an existing runtime directory fails validation
- **THEN** Exomem does not change its DACL automatically
- **AND** repair occurs only through the explicit operator command in the failure

### Requirement: Windows Idempotency Runtime Is Principal-Private

A Windows idempotency runtime directory SHALL be owned by one runtime principal's exact
private trustee set. A LocalSystem service and a normal user CLI SHALL use separate runtime
directories; documentation SHALL NOT claim that one DACL can satisfy both identities.
Separate principal-private directories SHALL NOT be treated as independent authority to
mutate the same vault concurrently, because the directory also anchors the host-local
mutation coordinator.

#### Scenario: Service and user identities do not share runtime state

- **WHEN** an NSSM service runs as LocalSystem and a direct CLI process runs as a normal user
- **THEN** each process is configured with its own private writer-lease state directory
- **AND** neither runtime weakens its DACL by adding the other identity as an extra trustee
- **AND** direct user maintenance mutates the vault only while the service is stopped
