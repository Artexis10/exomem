## Context

The owner wants procedures that work as dynamic skills. The owner writes, edits and retires them as ordinary pages. Any agent finds one by meaning, through task discovery in `recall_context`, and follows it. The agent needs a signal that the owner stands behind this exact text, and that signal must resist text that says "approve me".

Exomem already holds the parts. Compiled pages carry semantic units. The context-roles registry maps unit categories to `projection: method`. The activation packet projects methods with their source version, and the continuity token carries `method_versions`. The reserved-paths registry holds product-owned state, such as source corrections. This change adds one record and one verdict. It adds no new retrieval path.

## Decisions

### A procedure is an ordinary compiled page

A procedure is a compiled page that holds method units. The `procedure` note type only guides authoring: folder `Notes/Procedures`, optional sources, a `summary` that says when to use it, a "not for" section and procedure blocks. Code never branches on the `procedure` key. Followability is the approval verdict, so a page of any compiled type that holds method units can be approved.

- **Why.** Existing writers, search, history, supersession and access rules then apply with no new path. An owner can turn an old note into a procedure by approving it.
- **Rejected: a dedicated procedure store or a type that code recognises.** It forks the write path, and the type key would become a second source of authority.

### Approval is page-level and binds one exact version

The ledger entry binds the stable ref and the SHA-256 of the page bytes. Any change to the bytes makes the entry stale. The page needs a stable ref, which is its `exomem_id`.

- **Why.** The method projection already binds the whole-source version, because an exclusion outside the selected unit changes how the method applies. A page-level version covers exclusions, applicability and every unit.
- **Why a move keeps the approval.** An entry binds the ref and the bytes, and a move changes neither. A move or a trash restore with unchanged bytes keeps the approval, unless a later `revoke` entry exists.
- **Rejected: unit-level approval.** A unit's meaning depends on the rest of its page. A unit entry would multiply ledger entries and would let an edited exclusion leave an approval standing.

### Approval lives outside the page, in an authenticated ledger

A reserved tree, `_Approvals`, holds the ledger, one file per entry. The reserved-paths registry lists it as a vault-canonical descriptor with the owning command `approve_procedure`, as it lists source corrections. Generic, import and capture routes refuse that tree. Entries are appended and never rewritten.

Each entry carries an HMAC over its canonical bytes: every field except the MAC, including the schema version and the client label. The product generates the key, one per owner, and keeps it outside the vault in the state root, with mode 0600. No surface serves it. The reserved-paths registry lists the key file as an external-canonical descriptor, so nothing that rebuilds, resets, wipes or migrates machine-local state touches it. An entry whose MAC does not verify with this host's key is ignored for the verdict. The owner sees a finding that calls it foreign or tampered.

- **Why.** An approval inside the approved bytes is circular, and any writer, import, copy or captured text could set it. A ledger that only one owner-only operation writes cannot travel with text. The key keeps it from travelling with a vault copy too: a whole-vault import, a file sync or a clone carries `_Approvals` but not the key.
- **Consequence.** A vault that someone copies, imports, syncs or clones to another host carries no working approval. Every verdict there reads `unapproved`. The owner re-approves on the new host, or copies the state-root key from the owner's own backup. Hosts that sync one vault must share the key, and the owner copies it by hand. A revoke that another key signed does not verify on a host that lacks that key.
- **Rejected: a frontmatter flag.** Any route that writes the page can write the flag, and a copy carries it.
- **Rejected: a status class.** Status describes the page's life and any writer edits it. A copy keeps it, and a vault can overlay the status vocabulary. Approval is a verdict about the owner's endorsement, not a lifecycle state.
- **Rejected: a host-local ledger.** The key already ties approval to the host. The entries are the owner's audit record: who approved which version, and when. They belong with the vault's backup, and with a restored key they take effect again.
- **Rejected: an unauthenticated ledger in the vault.** A copied vault would then carry approvals to hosts where the owner never made them.

### Entries are single files that a rename publishes

Each entry is one file under `_Approvals/<ref>/`, named by its entry id. The product writes it to a temporary name in the same directory and renames it into place in one step. A crash leaves the whole entry or none. A torn temporary file is never an entry. The rename is the receipt, so the operation has no separate receipt-first lifecycle. The governance text for owning commands names that exception.

The latest entry for a ref is the verified entry with the greatest recorded UTC time, then the greatest entry id. The time comes from the host clock. The writer records a time later than every verified entry of that ref, so a slow clock cannot order a new entry before an old one. A skew between hosts matters only when two hosts write one ref before their copies of the vault meet.

Two kinds of bad entry exist, and they differ in what the server may do.

- **Foreign or tampered.** The entry parses, carries a known schema version and sits under the right path, but its MAC fails. The verdict ignores it, so it cannot approve and cannot revoke. The owner sees a finding. The server never moves it on its own, because a copied vault holds many such entries, and moving them would destroy the owner's record on the original host.
- **Unreadable.** The entry fails to parse, carries a newer schema version than this build knows, or sits under a path that does not match its content. It makes only its own ref `unapproved` and raises an owner-only finding. The entry's path names its ref, so a damaged file still names the ref it affects. It never blocks another ref and never blocks an approve or revoke.

An approve or revoke runs three steps in order. First it runs its guards: owner, eligibility for an approve, and `expected_version`. A failed guard refuses before any write or move. Second it runs the quarantine step, which moves that ref's unreadable files into a quarantine folder inside the same tree and reports them. Third it runs the repeat test and appends. A repeat after the quarantine step appends nothing, and the ref reads `approved`. The action `quarantine` does the move alone, for one ref or for every ref, and takes no `expected_version`. It moves only unreadable files, unless the owner names a ref. For a named ref it also moves that ref's foreign or tampered entries. A quarantined file is never read for a verdict.

A missing `_Approvals` tree means no approvals. Every verdict is `unapproved` and no finding appears, even on a host that holds a key.

- **Why one file per entry.** It follows source corrections and keeps a failure to one ref.
- **Rejected: one ledger file.** One damaged byte or one torn append would make every approval unreadable.

### One verdict function

`procedure_approval.py` holds the ledger, the eligibility test and the verdict. Every surface calls it.

1. If the page is not eligible, return `unapproved`. Eligible means compiled role, status class `live`, a stable `exomem_id` that resolves to one page, and at least one method unit.
2. If the caller's release of the page is below FULL, return `unapproved`.
3. Read the entries for the ref. If any is unreadable, return `unapproved` and raise the owner-only finding. Ignore a foreign or tampered entry and report it.
4. Take the latest verified entry. If there is none, or it is a `revoke`, return `unapproved`.
5. If the entry's version equals the page's current version, return `approved`.
6. Otherwise return `stale` to the owner and `unapproved` to everyone else.

- **Why `stale` needs eligibility.** A retired or superseded page is not a procedure to follow. It reads `unapproved`, not `stale`, so a re-approval is never offered for it.
- **Why `stale` is owner-only.** For the owner, `stale` is the useful state: it says to approve again. For anyone else it would reveal that the owner once approved the page. A non-owner sees two states and never the entry's fields.
- **Why an unreadable entry fails its ref.** A skipped entry could be a `revoke`, and the verdict would show a revoked page as approved. A foreign entry differs: it has no authority on this host, so ignoring it is the correct reading.
- **Why below FULL reads `unapproved`.** A reader that sees an excerpt cannot judge the page the owner endorsed, and the version belongs to the full bytes.

### Retire and supersede through ordinary writers

The owner retires a procedure by changing its status, and supersedes it by writing a successor. A page whose status class is not `live` is not approved. A successor starts unapproved. A revoke keeps the page as reference and appends one entry. A revoke takes the ref and an optional `expected_version`, so the owner can revoke a retired or deleted page. An approve requires `expected_version`.

### Delivery adds a label and one line, not steps

`task_context.methods` items gain `approval`. An approved item adds one short prose line, "Approved procedure: <title>. Read it, then follow it where it fits.", that the shared counter charges. `read_memory` reports the same verdict. The continuity `method_versions` hints carry `approval`, and revalidation resets held methods when it differs. Approve and revoke change no page bytes, so the version check alone would keep a revoked method alive. Approval adds no ranking.

- **Why a reset on a verdict change.** The existing reset invalidates a held projection when the source version changes. A verdict change needs the same effect without a byte change.
- **Rejected: inline steps in the packet.** Steps spend the budget and hide the page's exclusions. The agent reads the page, and the version it reads is the version that the verdict names.
- **Rejected: generated `SKILL.md` files.** A file installs per host, goes stale on an edit and cannot carry a verdict that changes without an edit. The open change `recognize-situations-and-project-methods` already rules out another static skill package.

### Safety contract

Approval is a label, not a command. The owner endorsed this version as their way of working. The agent decides whether it fits. The user's current instruction wins, and an approved procedure grants no authority to send, publish, spend, change access or install code. Captured material lands in Sources, which are never approvable. Compiled pages start unapproved. Generic, import and capture routes cannot write the ledger. Any edit makes the approval stale.

Agents never approve or revoke, and never write, move or delete under `_Approvals`, unless the user, in their own turn, asks for that exact procedure. The rule covers the MCP tool, the CLI and a direct file write.

The boundary has two parts. On an MCP-only client, the host's per-tool prompt for `approve_procedure` protects the ledger. On a client with shell access, an agent that runs as the vault owner's OS user can reach the CLI and the key file. Approval there is as strong as any other write that the owner's account can make, and the skill rule is the only guard. The design claims no stronger boundary than that.

The ledger can also roll back by deletion. Deleting a `revoke` file, or restoring an older vault backup on the same host, restores the earlier approval, because the earlier entries still verify. An agent with file access can do this as it can any owner write. The skill rule therefore covers write, move and delete. This change adds no high-water mark.

### Why a new operation

`approve_procedure` is its own tool so that a host can allow edits and still prompt for approvals. It is owner-only: the command registry classifies it in row 4 of the credential matrix of `authorization-session-binding`. It is not admitted on Cloud in this change. Its MCP annotations are not read-only, not destructive and idempotent. A repeat call appends nothing when the latest verified entry already holds the same action and version.

The approve result shows what changed in the page since the last version approved for that ref, taken from the page's retained history. It says when that version is not recoverable. The owner then sees what the endorsement newly covers.

## Controls

- **An unreadable entry returns `unapproved` for its ref.** It prevents an agent from treating an unendorsed or revoked procedure as endorsed. When it fires wrongly, one ref reads as reference and its content stays readable. The owner pays until a re-approval or a `quarantine`, and the cost stays bounded to that ref. Authority is the one place where fail-closed is right, because an unexpected entry means something is wrong.
- **An entry that fails its MAC is ignored.** It prevents a copied vault from carrying approvals. When it fires wrongly, the owner re-approves after a legitimate key loss or after a move to a host without the key. The owner pays once per procedure.
- **A page without a resolvable `exomem_id` is ineligible.** A duplicated id does not resolve under the existing identity contract, so no verdict can name one page. The page reads `unapproved` until the owner resolves the duplicate. The owner pays, and the refusal names the duplicate.
- **The owner approves by hand.** Automation cannot supply the endorsement, because the endorsement is the owner's judgement.

## Risks and trade-offs

- **Server-made rewrites mark approval stale.** A link rewrite or an `updated:` stamp changes bytes. We accept this and show `stale` to the owner. Excluding such fields from the version would make the version depend on which bytes the server may touch.
- **The tool surface changes.** The new tool changes the surface digest. Release needs the ChatGPT schema refresh, the schema budget bump and regenerated capabilities documentation.
- **A model may call approve on injected text.** The hostile real-model run decides this, with a shell-capable agent and with host prompts on. If deciding agents call `approve_procedure`, run the CLI or write a ledger file on injected text, remove the tool from MCP and from the generated CLI. Approval then goes through the terminal UI only, `exomem tui`, which requires a terminal. That route stops an agent that acts by tool call or by accident. It does not stop an agent that reads the key and writes an entry by hand, which is the boundary above. The ledger, the verdict and the delivery stay as specified.
- **Ledger growth.** The ledger grows by one file per owner action, so no compaction is planned. The verdict reads the entry files of the refs that a request names.
- **Store mode.** The open change `make-vault-store-canonical` makes a SQLite store canonical for pages and projects Markdown from it. The entry files are not pages. That change needs a store-mode rule for these non-page reserved files, so that they stay plain files that the projector neither imports as pages nor overwrites. Until that rule exists, the ledger is file-backed in both modes.
- **Sequencing.** The serving work extends the method projection of `recognize-situations-and-project-methods`. The `context-activation` delta adds a requirement and names none of that change's text, so either change can archive first.
