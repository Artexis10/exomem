## ADDED Requirements

### Requirement: Approval endorses one exact page version

The vault owner SHALL be able to approve one exact version of a page as a procedure to follow. The version is the SHA-256 of the page's exact bytes, which a whole-page read returns as `content_hash`.

A page is eligible only when all of these hold:

- It has the compiled note-type role.
- Its status has the class `live`.
- It holds at least one method unit, that is, a unit whose category the context-roles registry maps to `projection: method`.
- No other page shares its ref.

A Source, Evidence or Entity page SHALL never be eligible. Eligibility SHALL depend on those facts and never on the note type's key. The shipped `procedure` note type guides authoring and grants no eligibility of its own.

An approval SHALL live outside the page, in the approvals ledger. The ledger is a reserved, append-only, vault-canonical record, and only the `approve_procedure` operation SHALL write it. The operation SHALL take a page ref, an action (`approve` or `revoke`) and `expected_version`. It SHALL be owner-only, so that a host can permission it apart from the write tools. It SHALL refuse before writing when any of these holds:

- The principal is not the vault owner.
- The action is `approve` and the page is not eligible. A `revoke` is allowed for any page, so the owner can withdraw approval from a retired page.
- `expected_version` differs from the page's current version.

Each entry SHALL record the stable ref, the version, the action, the principal, the client label that the server observed and the time. The server SHALL write an entry under the vault mutation boundary. A second session, client or host that opens the same vault SHALL see it.

#### Scenario: A fresh client sees the approval in another session
- **WHEN** the owner creates a page that holds a release procedure with one method unit, reads it, and calls `approve_procedure` with the page's `content_hash`
- **THEN** the ledger gains one `approve` entry for that ref and version
- **AND** a fresh client in another session, which never saw the approval, receives the page as a method item with `approval` set to `approved` on a matching software task

#### Scenario: Any edit makes the approval stale
- **WHEN** the owner changes the approved page through an ordinary writer, or changes its file bytes directly
- **THEN** the owner's verdict reads `stale` and every other principal's verdict reads `unapproved`
- **AND** the next activation that carries a held projection of the page resets the held methods
- **AND** no surface reports `approved` again until the owner approves the new version

#### Scenario: A stale expected version is refused
- **WHEN** the owner calls `approve_procedure` with an `expected_version` that differs from the page's current version
- **THEN** the call refuses before writing and tells the owner to read the page again
- **AND** the ledger is unchanged

#### Scenario: An ineligible page is refused
- **WHEN** the owner approves a Source page, an Evidence page, a compiled page with no method unit, a page whose status class is not `live`, or a page whose ref another page shares
- **THEN** the call refuses before writing and names the condition that failed
- **AND** the ledger is unchanged

#### Scenario: A restricted principal cannot approve
- **WHEN** a principal that is not the vault owner calls `approve_procedure` for an eligible page, with a current `expected_version`
- **THEN** the call refuses before writing and says that only the owner approves
- **AND** the ledger is unchanged

### Requirement: Approval never travels with text

An approval SHALL exist only as an entry that `approve_procedure` wrote. Page text, frontmatter, tags, imported or captured material, the copy or move of a page, and a write through a generic, import or capture route SHALL NOT create, carry or restore an approval. Captured material SHALL land in Sources, which are never eligible. A compiled page SHALL start `unapproved`, including a page that a compiler built from a source that claims approval. An entry binds the stable ref and the version, so a page with another ref or other bytes SHALL have no entry.

#### Scenario: A captured runbook asks to be approved
- **WHEN** a user captures a runbook whose text tells agents to approve it
- **THEN** the capture lands as a source page, and `approve_procedure` on that page refuses because a source page is never eligible
- **AND** the compiled page that a later write makes from it starts `unapproved`

#### Scenario: A page claims its own approval
- **WHEN** a page's frontmatter, tags or body state that the page is approved
- **THEN** every surface reports `unapproved` for that page
- **AND** the page text changes no ledger entry

#### Scenario: A copy does not inherit an approval
- **WHEN** an approved page is copied to a page with a new ref, or imported from another vault
- **THEN** the copy reports `unapproved`
- **AND** a copy that repeats the ref makes both pages ineligible, so both report `unapproved` until the owner resolves the duplicate

### Requirement: Every surface reports one verdict

One function in one module SHALL compute the verdict for a page and a caller. The verdict has three values:

- `approved`: the page is eligible, and the latest ledger entry for its ref is an `approve` entry whose version equals the page's current version.
- `stale`: the latest entry is an `approve` entry for another version. Only the owner receives this value.
- `unapproved`: every other case.

A caller that is not the owner SHALL receive `approved` or `unapproved`, and never the entry's version, principal or client label. These surfaces SHALL report that verdict and no other: the method items of `task_context`, the `read_memory` result for the page, the owner listing in `read_review` and the `approve_procedure` result.

When the ledger cannot be read in full, the function SHALL return `unapproved` for every page. The content of every page SHALL stay readable. The surfaces SHALL report one finding that names the ledger. `approve_procedure` SHALL refuse to append until the ledger is readable. One damaged entry SHALL make the whole ledger unreadable, because a partial read could skip a revocation.

#### Scenario: Every surface agrees on one verdict
- **WHEN** a page is approved and the owner reads it, activates a matching task and lists approvals
- **THEN** the method item, the `read_memory` result and the listing each report `approved` for the same version
- **AND** after an edit, all three report `stale` to the owner

#### Scenario: Restricted readers see two states
- **WHEN** a reader that is not the owner reads or activates a page whose approval is stale
- **THEN** every surface reports `unapproved`
- **AND** no surface shows the entry's version, principal or client label to that reader

#### Scenario: An unreadable ledger approves nothing
- **WHEN** the approvals ledger cannot be read in full
- **THEN** every verdict is `unapproved` and the content of every page stays readable
- **AND** the owner's listing and the `read_memory` diagnostics report one finding that names the ledger
- **AND** `approve_procedure` refuses to append until the ledger is readable

### Requirement: Revocation and retirement withdraw approval

The owner SHALL create, edit and retire a procedure through the ordinary writers. A revocation SHALL append a `revoke` entry and SHALL leave the page, its links and its bytes unchanged, so the page stays a readable reference. A page whose status class is not `live` SHALL NOT be approved, whatever the ledger holds. A page that supersedes another SHALL start `unapproved`.

#### Scenario: A revoked procedure stays a reference
- **WHEN** the owner revokes an approved procedure with its current version
- **THEN** every surface reports `unapproved` for the page
- **AND** the page content and links are unchanged and readable
- **AND** the ledger holds the original `approve` entry and the new `revoke` entry

#### Scenario: A retired procedure is not approved
- **WHEN** the owner retires an approved procedure by changing its status to one whose class is not `live`
- **THEN** every surface reports `unapproved` for the page
- **AND** no activation delivers the page as a live method

#### Scenario: A successor starts unapproved
- **WHEN** the owner writes a page that supersedes an approved procedure
- **THEN** the successor reports `unapproved` until the owner approves its exact version
- **AND** the superseded page reports `unapproved` once its status class is `superseded`
