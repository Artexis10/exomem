## ADDED Requirements

### Requirement: Legacy Windows Runtime DACL Failures Are Actionable

The disclosure-evidence requirement "Governance Evidence Filesystem Safety And
Critical Durability Are Cross-Platform" refuses a pre-existing unsafe Windows
writer-state root. That refusal SHALL reach the operator as one readable line,
not as a bare pathless `RuntimeError`.

The same validation fault SHALL be visible to `exomem doctor`; an unreadable or unsafe
idempotency store SHALL NOT be reported as healthy.

#### Scenario: The refusal is one readable line

- **WHEN** an upgraded install opens a pre-existing idempotency runtime directory whose DACL
  is not protected or whose ACEs do not match the current principal-private trustee set
- **THEN** the error is one operator-readable line rather than a bare pathless `RuntimeError`

#### Scenario: Doctor surfaces the unsafe runtime

- **WHEN** `exomem doctor` inspects an idempotency runtime with an invalid Windows DACL
- **THEN** its idempotency-store check is not reported as passing
- **AND** the check includes the exact offending path and remediation command

### Requirement: Separate Windows Runtime Roots Grant No Shared Or Concurrent Authority

The disclosure-evidence requirement "Governance Evidence Filesystem Safety And
Critical Durability Are Cross-Platform" gives a LocalSystem service and a
normal-user CLI separate writer-state roots. Documentation SHALL NOT claim that
one DACL can satisfy both identities. Separate principal-private directories
SHALL NOT be treated as independent authority to mutate the same vault
concurrently, because the directory also anchors the host-local mutation
coordinator.

#### Scenario: Documentation does not offer one DACL for both identities

- **WHEN** documentation explains how an NSSM service running as LocalSystem and a direct CLI process running as a normal user share a host
- **THEN** it does not tell the operator to weaken either runtime DACL by adding the other identity as an extra trustee

#### Scenario: Direct user maintenance waits for the service

- **WHEN** an NSSM service running as LocalSystem and a direct CLI process running as a normal user each use their own runtime directory for one vault
- **THEN** direct user maintenance mutates the vault only while the service is stopped
