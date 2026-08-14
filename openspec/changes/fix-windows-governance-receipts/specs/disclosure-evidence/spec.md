## ADDED Requirements

### Requirement: Governance Evidence Filesystem Safety And Critical Durability Are Cross-Platform

Receipt evidence opening and creation SHALL use platform-safe, no-follow filesystem operations on every supported operating system. POSIX SHALL retain the receipt instance directory and use descriptor-relative child access. Windows SHALL retain the instance directory and its existing ancestors while a monthly JSONL child is opened or exclusively created. A monthly evidence handle SHALL resolve to a regular, non-reparse direct child of that retained instance directory.

Native Windows receipt operations SHALL NOT depend on the CRT opening a directory. They SHALL use native directory handles and validated child file handles while preserving the anti-symlink, anti-reparse, identity, and entry-swap guarantees of the POSIX directory-relative implementation. Ancestors SHALL be retained with metadata-only access; write access SHALL be requested only for the exact directory being flushed.

Before advancing the durable sidecar head for a critical receipt, Exomem SHALL flush the complete JSONL durable prefix and every receipt-directory entry through the existing Knowledge Base root. Native Windows SHALL perform this operation through a write-capable no-follow directory handle. Unsupported, denied, unsafe, or failed directory durability SHALL fail closed and SHALL NOT advance the durable or observed sidecar head.

A file-ahead critical suffix left by such a refusal SHALL remain eligible for the existing exact-ID retry and verified reconcile paths. Neither path SHALL duplicate the critical event or promote it before file and directory durability succeeds.

Native Windows lifecycle tombstone writes and unlinks plus deletion/recovery source and destination rename barriers SHALL use the same retained, no-follow, write-capable final-directory durability primitive rather than CRT directory opens. A lifecycle directory open, identity, or flush failure SHALL remain fail-closed at the existing checkpoint and SHALL NOT report the tombstone, unlink, deletion, recovery, or rename durable.

When deletion or recovery abort restores graph epoch state by removing a newly staged floor or checkpoint, native Windows SHALL flush that exact parent directory through the same primitive. The original lifecycle refusal SHALL be returned only after the prior graph state is durably restored. An epoch unlink or directory-flush failure SHALL remain a graph lifecycle rollback failure requiring reconciliation.

If the canonical rename succeeds but a following source or destination directory flush fails, the transition SHALL record that placement changed before propagating the durability refusal. Abort SHALL durably inverse-rename before restoring the prior graph epoch. If the inverse move or its durability barrier cannot be proven, the staged graph floor/checkpoint and lifecycle evidence SHALL remain for reconciliation and SHALL NOT be erased as though canonical placement were unchanged.

When one or more same-principal receipt or mutation-coordinator processes are the first owners of an absent native Windows writer-state root, Exomem SHALL establish the existing protected principal-private runtime DACL before creating any mutation-lock directory or file. The winning creator alone SHALL apply the DACL to the exact directory it created; concurrent same-principal losers SHALL tolerate atomic-create races and wait only a short bounded interval for the winner's DACL to validate. A pre-existing unsafe or different-principal root SHALL be refused with the exact offending path and remediation command, SHALL NOT be repaired implicitly, and SHALL NOT gain a lock artifact. LocalSystem service and normal-user direct CLI processes SHALL use separate writer-state roots.

#### Scenario: Native Windows appends and verifies ordinary evidence

- **WHEN** a governed disclosure appends a receipt in a valid native Windows vault
- **THEN** the monthly evidence file is opened or created without a CRT directory open
- **AND** chain verification succeeds
- **AND** the ordinary receipt makes no new power-loss durability claim

#### Scenario: Native Windows persists a critical receipt before its anchor

- **WHEN** a critical intent or terminal is appended in a valid native Windows vault
- **THEN** its JSONL bytes are flushed
- **AND** the receipt directory chain is flushed through the Knowledge Base root
- **AND** only then is the SQLite durable head committed

#### Scenario: Directory durability failure remains fail-closed

- **WHEN** a critical JSONL record is written but any required directory open or flush fails
- **THEN** the caller receives a content-free receipt refusal
- **AND** the durable and observed sidecar heads do not advance
- **AND** retry or reconcile must re-establish durability before promotion

#### Scenario: A reparse component cannot redirect evidence

- **WHEN** a receipt ancestor, instance directory, or monthly evidence entry is a Windows junction, symlink, or other reparse point
- **THEN** append and verification refuse it
- **AND** no outside file is read, changed, or created

#### Scenario: First instance creation cannot follow a reparse ancestor

- **WHEN** `_Governance`, `events`, or the first receipt instance path is a reparse point or is swapped during secure creation
- **THEN** instance creation refuses it
- **AND** no directory or evidence file is created in the reparse target or replacement tree

#### Scenario: A namespace swap cannot redirect a monthly open

- **WHEN** another actor attempts to rename or replace the retained instance path between validation and monthly-file open
- **THEN** retained handles either block the swap or Exomem detects the changed identity
- **AND** no bytes cross into the replacement tree

#### Scenario: Governed deletion and recovery persist lifecycle barriers

- **WHEN** native Windows performs governed deletion or recovery
- **THEN** its tombstone write or unlink and every source/destination rename directory are flushed at the existing lifecycle checkpoints
- **AND** critical receipt order and lifecycle lineage remain valid

#### Scenario: Ungoverned atomic rename persists its directories

- **WHEN** native Windows performs an allowed ungoverned lifecycle atomic rename
- **THEN** the source and distinct destination parent directories are flushed without a CRT directory open

#### Scenario: Lifecycle directory durability refuses unsafe state

- **WHEN** a lifecycle directory is a reparse point, changes identity, cannot be opened securely, or cannot be flushed
- **THEN** the operation fails closed with a content-free lifecycle error
- **AND** no later durability checkpoint is reported

#### Scenario: Lifecycle abort durably restores graph epoch state

- **WHEN** deletion or recovery refuses after staging a graph floor or checkpoint that was previously absent
- **THEN** abort removes the staged artifact and durably flushes its parent without a CRT directory open
- **AND** the original lifecycle refusal is preserved only after the exact prior graph state is restored
- **AND** a removal or flush failure reports graph lifecycle rollback failure instead

#### Scenario: Post-rename durability failure retains truthful graph state

- **WHEN** deletion or recovery atomically renames canonical content but a following source or destination directory flush fails
- **THEN** abort recognizes that canonical placement changed and attempts a durable inverse rename before restoring graph epoch artifacts
- **AND** successful inverse durability restores the exact prior placement and epoch before returning the original refusal
- **AND** inverse failure retains the graph floor/checkpoint and lifecycle marker for reconciliation rather than exposing a falsely current graph

#### Scenario: Receipt-first startup establishes the private runtime

- **WHEN** native Windows appends its first receipt with an absent configured writer-state root
- **THEN** the protected principal-private DACL is installed before any mutation-lock artifact
- **AND** the idempotency runtime can subsequently reuse the same valid root

#### Scenario: Concurrent same-principal first use converges

- **WHEN** multiple native Windows processes under the same principal race receipt append and mutation-coordinator lock access against the same absent writer-state root
- **THEN** one creator establishes the protected private DACL and every process uses that validated root
- **AND** no process repairs a directory entry it did not create
- **AND** every append and coordinator operation succeeds under its existing lock contract

#### Scenario: Coordinator-first startup uses the same private root

- **WHEN** a mutation coordinator is the first owner of an absent native Windows writer-state root
- **THEN** it establishes and validates the same protected private DACL before opening its lock
- **AND** later receipt and idempotency owners reuse that root successfully

#### Scenario: Receipt-first startup refuses a legacy unsafe runtime

- **WHEN** the configured writer-state root already exists with a non-conforming Windows DACL
- **THEN** receipt append fails with the exact root path and exact remediation command
- **AND** the DACL is unchanged
- **AND** no mutation-lock child is created

#### Scenario: Different principals do not share a private runtime

- **WHEN** a normal-user process is configured with a writer-state root secured for LocalSystem, or the inverse
- **THEN** validation refuses the exact trustee mismatch without changing the DACL
- **AND** the operator is directed to use the principal's separate configured state root or exact remediation
