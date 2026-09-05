## ADDED Requirements

### Requirement: State-root access is proven against the runtime principal, not the creating token

The private-DACL trustee set is token-relative, so a flow that creates or recreates
the machine-local state root SHALL resolve the **runtime principal** — the account
the server will run as — and apply that principal's private DACL, rather than the
DACL implied by the creating token. Where a service install exists, the runtime
principal SHALL be read from the platform service registry; where none exists, the
runtime principal is the current token and behaviour is unchanged. The system SHALL
NOT relax the expected trustee set, make the validator advisory, broadly grant the
operator on a live root, or silently rewrite the DACL of a root the process does not
own.

#### Scenario: A user-token creation leaves a root the service can open

- **WHEN** a flow running under the operator's token creates or recreates the state root on a host where the server is installed as a service under a different account
- **THEN** the resulting root satisfies the private-DACL validator evaluated for the service account
- **AND** the service opens it without a runtime DACL error

#### Scenario: No service install leaves current-token behaviour unchanged

- **WHEN** the same flow runs on a host with no service install
- **THEN** the runtime principal is the current token
- **AND** the root carries that token's private DACL exactly as before

#### Scenario: A failed registry read is not treated as an absent service

- **WHEN** the platform service registry cannot be read, as distinct from naming no service
- **THEN** the runtime principal degrades with the read failure recorded as its reason
- **AND** it does not present as a clean current-token resolution, which would fail a correctly sealed root and prescribe a repair that breaks the service

#### Scenario: A root owned by another principal is never silently re-ACLed

- **WHEN** a process encounters a state root governed by a principal it is not
- **THEN** it does not modify that root's descriptor
- **AND** it refuses with an actionable finding instead

#### Scenario: Only the state root the service is bound to is sealed

- **WHEN** a flow prepares a state root for a vault the installed service is not bound to
- **THEN** it does not apply the service principal's DACL to that root
- **AND** the operator retains access to the state root of a vault they created
- **AND** the presence of an unrelated service registration on the machine does not by itself authorise a re-ACL

#### Scenario: Sealing protects the state, not merely the directory entry

- **WHEN** a state root is sealed to the runtime principal after its state has been migrated in
- **THEN** the files already inside it are not left readable or writable by a principal the seal excludes
- **AND** the access check does not report the root as private while its contents remain accessible to an excluded principal

#### Scenario: A seal that did not take is not reported as success

- **WHEN** the seal writes a descriptor and the post-write read does not satisfy the runtime principal's own validator
- **THEN** the operation reports that the seal did not take
- **AND** it does not log or return a successful seal for that root

### Requirement: Doctor separates state placement from state access

`doctor` SHALL report state **access** as a check distinct from state **placement**,
because a correctly-placed root can still be unopenable and reporting only placement
hides that. The access check SHALL FAIL with a named, actionable finding — never an
unhandled exception — when the runtime principal cannot open the root, and SHALL
report a descriptor it could not evaluate as its own state rather than as a pass.
Checks that read state artifacts SHALL degrade to a finding rather than raising when
an artifact cannot be read, and SHALL name what was not evaluated.

#### Scenario: Cross-principal root fails with a finding, not a traceback

- **WHEN** `doctor` runs against a state root governed by a principal that is not the runtime principal the server will run as
- **THEN** the access check reports FAIL with the observed descriptor, the required trustees, and an exact remediation
- **AND** the process exits with a failing status rather than raising

#### Scenario: An unresolved runtime principal does not prescribe a token-relative repair

- **WHEN** the runtime principal could not be resolved and has degraded to the calling token
- **THEN** the finding says the runtime principal is unresolved
- **AND** it does not print an `icacls` command that would grant the calling token, because on a service install that repair breaks the principal the server actually runs as

#### Scenario: Placement passing does not imply access passing

- **WHEN** the state root is correctly placed outside the vault but unopenable by the runtime principal
- **THEN** the placement check may pass
- **AND** the access check independently reports FAIL

#### Scenario: A healthy service-owned root is not failed for the caller's sake

- **WHEN** `doctor` runs under an operator token against a root that is correct for the service account the server runs as
- **THEN** the access check PASSES, because the descriptor is judged against the runtime principal rather than the calling token
- **AND** the caller's own inability to open it is reported as expected detail, not as a failure

#### Scenario: A passing access check says when the contents were not evaluated

- **WHEN** the access check passes on a root whose listing this process is denied, so no child was examined
- **THEN** the finding states that the contents were not evaluated and that the verdict covers the directory entry only
- **AND** the sampled-child count is reported, so a reader can tell an unexamined root from a verified one

#### Scenario: An unreadable state artifact is reported, not fatal

- **WHEN** `doctor` cannot read the lexical sidecar, the deferred-index backlog, or list the rebuild-temp scan root
- **THEN** each affected check reports a finding naming the path it could not evaluate
- **AND** no check reports a confident negative result derived from an inspection that did not happen

#### Scenario: Access evaluation is a no-op off Windows

- **WHEN** `doctor` runs on a platform without Windows discretionary access control
- **THEN** the access check does not refuse
- **AND** it reports that the posture was not applicable rather than claiming the root is private

### Requirement: Maintenance refuses an unusable state root before taking any hold

A CLI maintenance operation against a state root the running process cannot work
with SHALL detect that condition up front and refuse, before acquiring the mutation
hold or performing partial work. Two distinct conditions qualify and BOTH SHALL
refuse: the process cannot open the root at all, and the process can open the root
but its own private-DACL validator rejects the trustee set. The second is not a
lesser case — it is how the failure presents to a LocalSystem service, which holds
an `SY` full-access ACE on an operator-created root and so opens it successfully
before failing closed at every private-state boundary behind it.

The refusal SHALL carry a stable machine-readable error code, the observed security
descriptor, the trustees the process requires, and an exact remediation command. A
maintenance operation SHALL NOT fail part-way through with a raw operating-system
error, and the pre-flight SHALL NOT itself raise one when its own inspection fails.

An unopenable root SHALL be distinguished by cause. Only an access denial is a
cross-principal condition with an ACL remediation; a reparse point or a wrong entry
type SHALL be reported as the path problem it is, without offering an ACL repair
that cannot fix it.

#### Scenario: Reconcile refuses before the mutation hold

- **WHEN** `maintain --reconcile --rebuild-graph` runs against a root owned by another principal
- **THEN** it exits non-zero with the stable cross-principal error code and remediation
- **AND** it has not taken the mutation hold or mutated any state

#### Scenario: An openable root the validator rejects refuses the same way

- **WHEN** maintenance runs as a principal that can open the root but whose private-DACL validator rejects its trustee set
- **THEN** it refuses up front with the same stable error code and remediation
- **AND** it does not proceed into state creation and fail there with a raw runtime DACL error

#### Scenario: An alias root is not reported as a principal problem

- **WHEN** the state root is a reparse point rather than a real directory
- **THEN** it is not classified as cross-principal
- **AND** the finding does not offer an ACL repair

#### Scenario: Offline state migration refuses the same way

- **WHEN** an offline state migration targets a root the current token cannot open
- **THEN** it refuses with the same error code and remediation before migrating any family

#### Scenario: A healthy same-principal root is not refused

- **WHEN** maintenance runs against a state root the current process can open normally
- **THEN** no cross-principal refusal occurs
- **AND** the operation proceeds exactly as before
