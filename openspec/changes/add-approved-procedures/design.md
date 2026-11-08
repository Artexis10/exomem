## Context

The owner wants procedures that work as dynamic skills. The owner writes, edits and retires them as ordinary pages. Any agent finds one by meaning, through task discovery in `recall_context`, and follows it. The agent needs a signal that the owner stands behind this exact text, and that signal must resist text that says "approve me".

Exomem already holds the parts. Compiled pages carry semantic units. The context-roles registry maps unit categories to `projection: method`. The activation packet projects methods with their source version, and the continuity token carries `method_versions`. The reserved-paths registry holds product-owned vault state, such as source corrections. This change adds one record and one verdict. It adds no new retrieval path.

## Decisions

### A procedure is an ordinary compiled page

A procedure is a compiled page that holds method units. The `procedure` note type only guides authoring: folder `Notes/Procedures`, optional sources, a `summary` that says when to use it, a "not for" section and procedure blocks. Code never branches on the `procedure` key. Followability is the approval verdict, so a page of any compiled type that holds method units can be approved.

- **Why.** Existing writers, search, history, supersession and access rules then apply with no new path. An owner can turn an old note into a procedure by approving it.
- **Rejected: a dedicated procedure store or a type that code recognises.** It forks the write path, and the type key would become a second source of authority.

### Approval is page-level and binds one exact version

The ledger entry binds the stable ref and the SHA-256 of the page bytes. Any change to the bytes makes the entry stale.

- **Why.** The method projection already binds the whole-source version, because an exclusion outside the selected unit changes how the method applies. A page-level version covers exclusions, applicability and every unit.
- **Rejected: unit-level approval.** A unit's meaning depends on the rest of its page. A unit entry would multiply ledger entries and would let an edited exclusion leave an approval standing.

### Approval lives outside the page, in an append-only ledger

A reserved tree, `_Approvals`, holds the ledger. The reserved-paths registry lists it as a vault-canonical descriptor owned by `approve_procedure`, as it lists source corrections. Generic, import and capture routes refuse that tree. Entries are appended and never rewritten.

- **Why.** An approval inside the approved bytes is circular, and any writer, import, copy or captured text could set it. A ledger that only one owner-only operation writes cannot travel with text.
- **Rejected: a frontmatter flag.** Any route that writes the page can write the flag, and a copy carries it.
- **Rejected: a status class.** Status describes the page's life and any writer edits it. A copy keeps it, and a vault can overlay the status vocabulary. Approval is a verdict about the owner's endorsement, not a lifecycle state.
- **Rejected: a host-local store.** A fresh client in another session or on another host must see the approval. The ledger sits in the vault, so it follows backup, export and every client of that vault.

### One verdict function

`procedure_approval.py` holds the ledger, the eligibility test and the verdict. Every surface calls it.

1. If the ledger cannot be read in full, return `unapproved` and report a finding.
2. If the page is not eligible, return `unapproved`. Eligible means compiled role, status class `live`, at least one method unit and a unique ref.
3. Find the latest ledger entry for the ref. If there is none, or it is a `revoke`, return `unapproved`.
4. If the entry's version equals the page's current version, return `approved`.
5. Otherwise return `stale` to the owner and `unapproved` to everyone else.

- **Why `stale` is owner-only.** For the owner, `stale` is the useful state: it says to approve again. For anyone else it would reveal that the owner once approved the page. A non-owner sees two states and never the entry's fields.
- **Why one damaged entry makes the ledger unreadable.** A partial read could skip a `revoke` entry and show a revoked page as approved.

### Retire and supersede through ordinary writers

The owner retires a procedure by changing its status, and supersedes it by writing a successor. A page whose status class is not `live` is not approved. A successor starts unapproved. A revoke keeps the page as reference and appends one entry.

### Delivery adds a label and one line, not steps

`task_context.methods` items gain `approval`. An approved item adds one short prose line, "Approved procedure: <title>. Read it, then follow it where it fits.", that the shared counter charges. `read_memory` reports the same verdict. The continuity `method_versions` hints carry `approval`, and revalidation resets held methods when it differs. Approve and revoke change no page bytes, so the version check alone would keep a revoked method alive. Approval adds no ranking.

- **Why a reset on a verdict change.** The existing reset invalidates a held projection when the source version changes. A verdict change needs the same effect without a byte change.
- **Rejected: inline steps in the packet.** Steps spend the budget and hide the page's exclusions. The agent reads the page, and the version it reads is the version that the verdict names.
- **Rejected: generated `SKILL.md` files.** A file installs per host, goes stale on an edit and cannot carry a verdict that changes without an edit. The open change `recognize-situations-and-project-methods` already rules out another static skill package.

### Safety contract

Approval is a label, not a command. The owner endorsed this version as their way of working. The agent decides whether it fits. The user's current instruction wins, and an approved procedure grants no authority to send, publish, spend, change access or install code. Captured material lands in Sources, which are never approvable. Compiled pages start unapproved. Generic, import and capture routes cannot write the ledger. Any edit makes the approval stale. The agent calls `approve_procedure` only when the owner asks in the current turn.

### Why a new operation

`approve_procedure` is its own tool so that a host can allow edits and still prompt for approvals. It takes `expected_version` on both actions. A revoke records which version the owner withdrew, and the cost is one read.

## Controls

- **Unreadable ledger returns `unapproved`.** It prevents an agent from treating an unendorsed or revoked procedure as endorsed. When it fires wrongly, an approved procedure reads as reference, and its content stays readable. The owner pays until the finding is fixed. Authority is the one place where fail-closed is right, because an unexpected ledger state means something is wrong.
- **A shared ref makes both pages ineligible.** It prevents a copy from inheriting an approval. When it fires wrongly, a duplicate removes an approval until the owner resolves it. The owner pays, and the refusal names the duplicate.
- **The owner approves by hand.** Automation cannot supply the endorsement, because the endorsement is the owner's judgement.

## Risks and trade-offs

- **Server-made rewrites mark approval stale.** A link rewrite or an `updated:` stamp changes bytes. We accept this and show `stale` to the owner. Excluding such fields from the version would make the version depend on which bytes the server may touch.
- **The tool surface changes.** The new tool changes the surface digest. Release needs the ChatGPT schema refresh, the schema budget bump and regenerated capabilities documentation.
- **A model may call approve on injected text.** The hostile real-model run decides this. If deciding agents call `approve_procedure` on injected text, even with host prompts on, remove the tool from MCP. Approval then goes through the human routes only, the CLI and the owner's own surface. The ledger, the verdict and the delivery stay as specified.
- **Ledger growth.** The ledger grows by one entry per owner action, so no compaction is planned. The verdict reads the ledger once per request.
- **Sequencing.** The serving work extends the method projection of `recognize-situations-and-project-methods`. The `context-activation` delta adds a requirement and names none of that change's text, so either change can archive first.
