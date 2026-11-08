## MODIFIED Requirements

### Requirement: Reserved Administration Trees Have Exclusive Owning Commands

`_Governance` SHALL be authorable only through the validated, receipt-first
`govern_memory` lifecycle. `_Consolidation` SHALL be reserved exclusively for the
consolidation command that owns its run state; until that command exists, no public
command SHALL mutate or enumerate it. `_Approvals` SHALL be reserved exclusively for
`approve_procedure`, the owner-only operation that owns the approvals ledger. The operation
SHALL append approval and revocation entries, SHALL NOT rewrite or delete one, and SHALL move
an entry file aside only through its `quarantine` action or through the quarantine step of an
`approve` or `revoke`. Owner identity, L6 disclosure, Tier-2 enablement,
an idempotency key, a filesystem alias, or a caller-provided bypass flag MUST NOT grant
generic administration-tree authority.

Generic create, directory-create, edit, observe, replace, append, move, delete, recover,
dataset, import, capture, Records, media, frame, upload/download/transfer, repair, and multiplexed alias
operations SHALL refuse before touching any of these trees. Read/list/search routes SHALL treat
them as structurally absent, except that the approval verdict and the `procedure_approvals`
listing of `read_review` SHALL read the approvals ledger. Both source and destination SHALL be checked for moves;
trash source, requested restore target, metadata-derived original target, and every
recursive child SHALL be checked for recovery. Owning commands SHALL receive a private,
non-serializable authority from the dispatcher and SHALL remain subject to their normal
proposal, receipt, crash-recovery, principal, and projection contracts.

The dispatcher classifier is preflight only. Every generic leaf SHALL traverse and
mutate relative to held no-follow vault/parent handles through the leaf operation and
revalidate stable filesystem identity at the read/mutation operation against retained
anchors. Stable pre-existing and anchor-observable parent swaps, rename/link changes,
hard links, reparse/junction points, and bind aliases SHALL refuse reserved-tree access.
A platform without equivalent handle-relative primitives SHALL disable the affected
route. The private owning-command authority
MAY enter its tree only through the same safe traversal plus its receipt-first lifecycle. The
exception is `approve_procedure`, which SHALL write each approvals entry as one atomic
whole-file rename that is itself the entry's receipt, with no separate receipt-first lifecycle.

#### Scenario: Generic write cannot author governance policy

- **WHEN** the owner uses any generic create, edit, append, replace, move, or alias route
  to target a file under `_Governance`
- **THEN** the operation refuses before filesystem mutation and directs the caller to
  the reviewed governance lifecycle

#### Scenario: Recovery cannot restore into a reserved tree

- **WHEN** a trash sidecar, explicit restore target, alias, or recursive child would
  restore into `_Governance` or `_Consolidation`
- **THEN** ordered preflight refuses before that entry is restored; prior durable saga
  entries reconcile from recorded receipt/journal state; recovery remains per-entry
  rather than transactional across its full set

#### Scenario: Dataset and media leaves cannot reach administration state

- **WHEN** a generic dataset, Records, media, frame, transfer, or repair leaf targets a
  reserved tree directly or through an alias
- **THEN** it returns no bytes, rows, frames, metadata, count, or mutation effect

#### Scenario: Govern memory retains exclusive policy authority

- **WHEN** a valid owner-approved governance commit passes proposal, fingerprint,
  receipt, and activation guards
- **THEN** its private dispatcher authority may update `_Governance` and no generic
  public flag can reproduce that authority

#### Scenario: Generic and import routes cannot write an approval

- **WHEN** a generic create, edit, append, replace, move, import, capture, or alias route, or a
  recovery, targets a path under `_Approvals`
- **THEN** the operation refuses before filesystem mutation and names `approve_procedure`
- **AND** no approval verdict changes

#### Scenario: Approve procedure retains exclusive ledger authority

- **WHEN** the owner calls `approve_procedure` for an eligible page with its current version
- **THEN** its private dispatcher authority writes one entry file under `_Approvals` by atomic rename
- **AND** no generic public flag can reproduce that authority

#### Scenario: Consolidation root is reserved before its owner exists

- **WHEN** an ordinary command targets `_Consolidation` before `consolidate_memory` is
  implemented
- **THEN** the path remains unavailable and no fallback writer claims it
