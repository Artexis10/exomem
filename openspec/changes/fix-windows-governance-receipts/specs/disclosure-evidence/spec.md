## ADDED Requirements

### Requirement: Receipt Filesystem Safety And Critical Durability Are Cross-Platform

Receipt evidence opening and creation SHALL use platform-safe, no-follow filesystem operations on every supported operating system. POSIX SHALL retain the receipt instance directory and use descriptor-relative child access. Windows SHALL retain the instance directory and its existing ancestors while a monthly JSONL child is opened or exclusively created. A monthly evidence handle SHALL resolve to a regular, non-reparse direct child of that retained instance directory.

Native Windows receipt operations SHALL NOT depend on the CRT opening a directory. They SHALL use native directory handles and validated child file handles while preserving the anti-symlink, anti-reparse, identity, and entry-swap guarantees of the POSIX directory-relative implementation. Ancestors SHALL be retained with metadata-only access; write access SHALL be requested only for the exact directory being flushed.

Before advancing the durable sidecar head for a critical receipt, Exomem SHALL flush the complete JSONL durable prefix and every receipt-directory entry through the existing Knowledge Base root. Native Windows SHALL perform this operation through a write-capable no-follow directory handle. Unsupported, denied, unsafe, or failed directory durability SHALL fail closed and SHALL NOT advance the durable or observed sidecar head.

A file-ahead critical suffix left by such a refusal SHALL remain eligible for the existing exact-ID retry and verified reconcile paths. Neither path SHALL duplicate the critical event or promote it before file and directory durability succeeds.

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
