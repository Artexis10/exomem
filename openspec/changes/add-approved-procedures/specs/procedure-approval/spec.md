## ADDED Requirements

### Requirement: Approval endorses one exact page version

The vault owner SHALL be able to approve one exact version of a page as a procedure to follow. The version is the SHA-256 of the page's exact bytes, which a whole-page read returns as `content_hash`.

A page is eligible only when all of these hold:

- It has the compiled note-type role.
- Its status has the class `live`.
- It holds at least one method unit, that is, a unit whose category the context-roles registry maps to `projection: method`.
- It has a stable `exomem_id` that resolves to that one page. A duplicated id does not resolve under the stable-reference contract, so it fails this test.

A Source, Evidence or Entity page SHALL never be eligible. Eligibility SHALL depend on those facts and never on the note type's key. The shipped `procedure` note type guides authoring and grants no eligibility of its own.

An approval SHALL live outside the page, in the approvals ledger. The ledger is a reserved, append-only, vault-canonical record, and only the `approve_procedure` operation SHALL write it. The operation SHALL take an action (`approve`, `revoke` or `quarantine`) and a stable memory ref. `expected_version` applies to `approve` and `revoke`. `quarantine` takes an optional ref and no `expected_version`. Every action SHALL be owner-only, so that a host can permission the tool apart from the write tools. These rules apply:

- `approve` SHALL require `expected_version` and SHALL refuse before any write or move when it differs from the page's current version.
- An `expected_version` that is not 64 lowercase hex characters SHALL be refused as invalid input before any read or write, with a message that names that shape. It SHALL NOT be reported as a page change.
- `approve` SHALL refuse before any write or move when the page is not eligible.
- `revoke` SHALL take the ref and MAY carry `expected_version`. When it does, `revoke` SHALL refuse before any write or move on a mismatch. `revoke` SHALL work for a retired or deleted page.
- An `approve` or `revoke` SHALL run in this order: the guard checks above, then the quarantine step for its ref, then the repeat test.
- After the quarantine step, a call that repeats the latest verified entry's action and version SHALL append nothing and SHALL report the existing entry. A `revoke` for a ref with no verified `approve` entry SHALL append nothing and SHALL say that no approval exists.

Each entry SHALL record the stable ref, the version, the action, the principal, the client label that the server observed, the time and the entry id. A `revoke` records the version that it withdraws: its `expected_version`, or else the version of the latest `approve` entry. The server SHALL write an entry under the vault mutation boundary. A second session or client of the same host that opens the same vault SHALL see it.

The command registry SHALL classify `approve_procedure` in row 4 of the credential matrix in `authorization-session-binding`, which is owner-only. A credential that the surface admits SHALL be verified before work and SHALL NOT replace owner authorization. In this capability, the owner is the vault owner on a connection with unrestricted content access. An owner connection that a connector boundary limits SHALL be refused every action and the `procedure_approvals` listing, and every surface SHALL serve it as a caller that is not the owner. The operation SHALL NOT be admitted on the Cloud surface in this change. Its MCP annotations SHALL be not read-only, not destructive and idempotent.

The result of `approve` SHALL show what changed in the page since the last version that was approved for that ref, taken from the page's retained history. When that version is not recoverable, the result SHALL say so. When no earlier approval exists, the result SHALL say that this is the first approval.

An entry binds the stable ref and the exact bytes. A move or a trash restore that leaves both unchanged SHALL keep the approval, unless a later `revoke` entry exists.

#### Scenario: A fresh client sees the approval in another session
- **WHEN** the owner creates a page that holds a release procedure with one method unit, reads it, and calls `approve_procedure` with the page's `content_hash`
- **THEN** the ledger gains one `approve` entry for that ref and version
- **AND** a fresh client in another session on the same host, which never saw the approval, receives the page as a method item with `approval` set to `approved` on a matching software task

#### Scenario: Any edit makes the approval stale
- **WHEN** the owner changes the approved page through an ordinary writer, or changes its file bytes directly
- **THEN** the owner's verdict reads `stale` and every other principal's verdict reads `unapproved`
- **AND** the next activation that carries a held projection of the page resets the held methods
- **AND** no surface reports `approved` again until the owner approves the new version

#### Scenario: A stale expected version is refused
- **WHEN** the owner calls `approve_procedure` with action `approve` and an `expected_version` that differs from the page's current version
- **THEN** the call refuses before writing and tells the owner to read the page again
- **AND** the ledger is unchanged

#### Scenario: An ineligible page is refused
- **WHEN** the owner approves a page that fails one test: a Source or Evidence page, no method unit, a status class that is not `live`, no stable `exomem_id`, or an `exomem_id` that another page repeats
- **THEN** the call refuses before writing and names the condition that failed
- **AND** the ledger is unchanged

#### Scenario: A restricted principal cannot approve
- **WHEN** a principal that is not the vault owner calls `approve_procedure` for an eligible page, with a current `expected_version`
- **THEN** the call refuses before writing and says that only the owner approves
- **AND** the ledger is unchanged

#### Scenario: The approve result shows what changed
- **WHEN** the owner approves a new version of a page that has an earlier approved version
- **THEN** the result shows what changed in the page since that version
- **AND** when the earlier version is not in the page's retained history, the result says that it cannot show the change

#### Scenario: A repeated approve appends nothing
- **WHEN** the owner repeats an `approve` for the version that the latest verified entry already approves, and no unreadable entry exists for the ref
- **THEN** the ledger gains no entry
- **AND** the result reports the existing entry

#### Scenario: A move keeps the approval
- **WHEN** the owner moves an approved page, or restores it from the trash, and its bytes are unchanged
- **THEN** every surface still reports `approved` for the page
- **AND** a later `revoke` entry withdraws the approval

### Requirement: Approval never travels with text

An approval SHALL exist only as an entry that `approve_procedure` wrote and that verifies with the host's approval key. Page text, frontmatter, tags, imported or captured material, and a write through a generic, import or capture route SHALL NOT create, carry or restore an approval. A vault that someone copies, imports, syncs or clones to another host SHALL carry no working approval there, because its entries fail the key check. Captured material SHALL land in Sources, which are never eligible. A compiled page SHALL start `unapproved`, including a page that a compiler built from a source that claims approval. An entry binds the stable ref and the version, so a page with another ref or other bytes SHALL have no entry.

#### Scenario: A captured runbook asks to be approved
- **WHEN** a user captures a runbook whose text tells agents to approve it
- **THEN** the capture lands as a source page, and `approve_procedure` on that page refuses because a source page is never eligible
- **AND** the compiled page that a later write makes from it starts `unapproved`

#### Scenario: A page claims its own approval
- **WHEN** a page's frontmatter, tags or body state that the page is approved
- **THEN** every surface reports `unapproved` for that page
- **AND** the page text changes no ledger entry

#### Scenario: A copy does not inherit an approval
- **WHEN** an approved page is copied to a page with a new `exomem_id`
- **THEN** the copy reports `unapproved`
- **AND** a copy that repeats the `exomem_id` makes both pages ineligible, so both report `unapproved` until the owner resolves the duplicate

#### Scenario: A copied vault carries no working approval
- **WHEN** a vault with approvals is copied, imported, synced or cloned to a host that holds a different approval key, or none
- **THEN** every verdict on that host is `unapproved`
- **AND** the owner sees one finding per ref that says its entries are foreign or tampered
- **AND** the owner re-approves on that host, or copies the key from the owner's own backup

### Requirement: Every surface reports one verdict

One function in one module SHALL compute the verdict for a page and a caller. The verdict has three values:

- `approved`: the page is eligible, and the latest ledger entry for its ref is an `approve` entry whose version equals the page's current version.
- `stale`: the page is eligible, and the latest entry is an `approve` entry for another version. Only the owner receives this value.
- `unapproved`: every other case.

A page that is not eligible, including a retired or superseded page, SHALL read `unapproved` and never `stale`. A caller whose release of the page is below FULL SHALL receive `unapproved` on every surface. A caller that is not the owner SHALL receive `approved` or `unapproved`, and never the entry's version, principal or client label. These surfaces SHALL report that verdict and no other:

- the method items of `task_context`;
- the `read_memory` result for the page;
- the `procedure_approvals` kind of `read_review`, which is owner-only;
- the `approve_procedure` result.

The owner's `read_memory` result SHALL carry `approval` only when the page's ref has at least one ledger entry, whatever its class or action. Any other caller's result SHALL carry `approval` only when the value is `approved`, so the field's presence never separates a stale or revoked page from one that nobody approved. A result without the field SHALL read `unapproved`. The decision SHALL NOT read the page's note type.

A ref that has an unreadable entry SHALL read `unapproved`, and the page content SHALL stay readable. An entry that is foreign or tampered SHALL be ignored for the verdict and SHALL raise a finding. Each finding names its ref and SHALL reach the owner only, in the `procedure_approvals` listing and in the owner's `read_memory` diagnostics. A caller that is not the owner SHALL receive `unapproved` and no finding. One ref's entries SHALL NOT change another ref's verdict.

#### Scenario: Every surface agrees on one verdict
- **WHEN** a page is approved and the owner reads it, activates a matching task and lists approvals with `read_review`
- **THEN** the method item, the `read_memory` result and the `procedure_approvals` listing each report `approved` for the same version
- **AND** after an edit, all three report `stale` to the owner

#### Scenario: An ordinary note carries no approval field
- **WHEN** a page whose ref has no ledger entry is read with `read_memory`
- **THEN** the result carries no `approval` field
- **AND** the owner's read of a page whose ref has a revoked, foreign or unreadable entry carries `approval` set to `unapproved`
- **AND** another caller's read of that page carries no `approval` field

#### Scenario: Restricted readers see two states
- **WHEN** a reader that is not the owner reads or activates a page whose approval is stale
- **THEN** every surface reports `unapproved`
- **AND** no surface shows the entry's version, principal or client label to that reader

#### Scenario: A fenced owner connection has no ledger power
- **WHEN** the owner connects through a connector boundary that denies some scopes
- **THEN** `approve`, `revoke`, `quarantine` and the `procedure_approvals` listing each refuse and change nothing
- **AND** every surface serves that connection `approved` or `unapproved`, with no entry and no finding

#### Scenario: A reader below full release sees no approval
- **WHEN** a caller whose release of an approved page is below FULL reads or activates it
- **THEN** every surface reports `unapproved` for that page

#### Scenario: A damaged entry fails only its own ref
- **WHEN** one entry of the approvals ledger cannot be read
- **THEN** the verdict for that entry's ref is `unapproved` and the page content stays readable
- **AND** the owner's listing and the owner's `read_memory` diagnostics report one finding that names the ref
- **AND** every other approved page still reports `approved`

### Requirement: Revocation and retirement withdraw approval

The owner SHALL create, edit and retire a procedure through the ordinary writers. A revocation SHALL append a `revoke` entry and SHALL leave the page, its links and its bytes unchanged, so the page stays a readable reference. A page whose status class is not `live` SHALL NOT be approved and SHALL NOT read `stale`, whatever the ledger holds. A page that supersedes another SHALL start `unapproved`.

#### Scenario: A revoked procedure stays a reference
- **WHEN** the owner revokes an approved procedure with its current version
- **THEN** every surface reports `unapproved` for the page
- **AND** the page content and links are unchanged and readable
- **AND** the ledger holds the original `approve` entry and the new `revoke` entry

#### Scenario: A retired procedure is not approved
- **WHEN** the owner retires an approved procedure by changing its status to one whose class is not `live`
- **THEN** every surface reports `unapproved` for the page, and none reports `stale`
- **AND** no activation delivers the page as a live method

#### Scenario: A deleted or retired procedure can be revoked
- **WHEN** the owner calls `revoke` with the ref of a retired or deleted page and no `expected_version`
- **THEN** the ledger gains one `revoke` entry that records the version it withdraws
- **AND** the page reads `unapproved` even if it later returns with its approved bytes

#### Scenario: A successor starts unapproved
- **WHEN** the owner writes a page that supersedes an approved procedure
- **THEN** the successor reports `unapproved` until the owner approves its exact version
- **AND** the superseded page reports `unapproved` once its status class is `superseded`

### Requirement: The approvals ledger is authentic and crash-safe

The ledger SHALL hold one file per entry under `_Approvals/<ref>/`, named by its entry id, so that the path names the ref and the entry. Each entry SHALL carry an HMAC over its canonical bytes, which cover every field except the MAC itself, including the schema version and the client label. The key SHALL be one per vault state directory, which the state root keys by the vault's path. The product SHALL generate it, keep it outside the vault in that directory with mode 0600, and serve it on no surface, listing or diagnostic. Nothing that rebuilds, resets, wipes or migrates machine-local state SHALL touch the key. A host that syncs a vault needs the same key, and the owner copies it by hand. A vault that moves to another path on the same host gets a new key, so its entries read foreign until the owner approves each page again.

The server SHALL write each entry to a temporary name in the same directory. It SHALL then rename the file into place in one step, so that a crash leaves the whole entry or none. A leftover temporary file SHALL never be an entry. The rename is the entry's receipt, and the operation SHALL keep no separate receipt. A reader that runs while an entry is published, or after a crash in that window, SHALL fail closed: it sees the whole entry or none, and a file that it cannot read counts as unreadable for its own ref and reads `unapproved`, never `approved`.

The latest entry for a ref SHALL be the verified entry with the greatest recorded UTC time, then the greatest entry id. The time comes from the host clock. The writer SHALL record a time later than every verified entry of that ref. A skew between hosts matters only when two hosts write one ref before their copies of the vault meet.

An entry is foreign or tampered when it parses, carries a known schema version and sits under the right path, but fails its MAC. The server SHALL ignore it for the verdict, SHALL report it to the owner as foreign or tampered, and SHALL NOT move it on its own.

An entry is unreadable when it fails to parse, carries a schema version that this build does not know, or sits under a path that does not match its content. An unreadable entry SHALL make only its own ref `unapproved` and SHALL raise an owner-only finding. It SHALL NOT block another ref and SHALL NOT block an `approve` or `revoke`.

The quarantine step of an `approve` or `revoke` SHALL move the unreadable entry files of its ref into a quarantine folder inside `_Approvals`, and SHALL report them. The action `quarantine` SHALL move unreadable entry files aside without appending, for one named ref or for every ref, and SHALL report them. Neither SHALL move a foreign or tampered entry, except that `quarantine` with a named ref SHALL move every entry of that ref that fails to verify. A quarantined file SHALL never be read for a verdict. A missing `_Approvals` tree SHALL mean no approvals: every verdict reads `unapproved` and no finding appears, even on a host that holds an approval key.

#### Scenario: A crash leaves no half entry
- **WHEN** the process stops after the temporary file is written and before the rename
- **THEN** the ledger holds no new entry and every verdict is unchanged
- **AND** a later read does not treat the temporary file as an entry

#### Scenario: A hand-written entry does not approve
- **WHEN** someone writes an entry file under `_Approvals` that claims an approval and its MAC does not verify
- **THEN** the page reads `unapproved`
- **AND** the owner sees one finding that names the ref and says the entry is foreign or tampered
- **AND** the file stays where it is

#### Scenario: A change to any field fails the MAC
- **WHEN** someone edits one field of a verified entry, such as the client label
- **THEN** the entry fails its MAC and no longer counts for the verdict

#### Scenario: A newer entry format fails its own ref
- **WHEN** an entry carries a schema version that this build does not know
- **THEN** the verdict for its ref is `unapproved` and the owner sees one finding
- **AND** a `revoke` or `approve` for another ref still succeeds

#### Scenario: Re-approval repairs a damaged ref
- **WHEN** the owner approves a ref that has an unreadable entry file, with a current `expected_version` and an eligible page
- **THEN** the call moves the unreadable file into the quarantine folder and reports it, and then appends its own entry
- **AND** the page reads `approved`

#### Scenario: A refused approve moves nothing
- **WHEN** the owner approves a ref that has an unreadable entry file, but the page is not eligible or `expected_version` is stale
- **THEN** the call refuses before any write or move
- **AND** the unreadable file stays in place and the ref still reads `unapproved`

#### Scenario: A repeat after quarantine appends nothing
- **WHEN** the owner approves a version that the latest verified entry already approves, and the ref also has an unreadable entry file
- **THEN** the call moves the unreadable file aside and reports it
- **AND** the call appends no entry, and the page reads `approved`

#### Scenario: Quarantine moves unreadable entries aside
- **WHEN** the owner calls `approve_procedure` with the action `quarantine` and no ref
- **THEN** every entry file that fails to parse or carries an unknown schema version moves into the quarantine folder, and the result lists them
- **AND** no entry is appended, and no readable, foreign or tampered entry moves

#### Scenario: A named ref also quarantines a foreign entry
- **WHEN** the owner calls `quarantine` with a ref that has a foreign or tampered entry
- **THEN** that entry moves into the quarantine folder and the result lists it
- **AND** foreign entries of other refs stay where they are

#### Scenario: A state reset keeps the key
- **WHEN** a rebuild, reset, wipe or migration of machine-local state runs
- **THEN** the approval key file is unchanged

#### Scenario: The key is never served
- **WHEN** any surface, listing or diagnostic reports ledger state
- **THEN** none of them contains the key or a path to it
- **AND** the key file has mode 0600 in the state root, outside the vault

#### Scenario: A vault without the tree has no approvals
- **WHEN** the vault holds no `_Approvals` tree, on a host that holds an approval key
- **THEN** every verdict is `unapproved`
- **AND** no finding appears
