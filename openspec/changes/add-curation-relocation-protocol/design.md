## Context

Curation refuses every relocation step because a crash between a rename and
its witness could not be told apart from a crash followed by an outside
inverse rename. The former design closed that gap with a durable
parent-namespace generation token. No supported filesystem exposes one, so
that design could never start.

Two further mechanisms stop a rehome even outside curation. A move refuses with
`APPEND_ONLY` when it would rewrite a link inside a Source or Evidence page.
The resolver forgets a page's old path, so any link that Exomem cannot rewrite
dangles. The owner ruled on 2026-11-16 that the content and provenance of
Sources and Evidence never change, and that their pointers change only through
a governed, approved, proven, audited and reversible write.

This design replaces the token protocol with page lineage, a governed pointer
write and rename-free recovery.

**Goals:** a page moves, alone or in a curation plan, without a dangling link;
an append-only page changes only in its pointer spans, with proof and an
exact revert; a crash during a curation move never causes a second rename and
never loses lineage.

**Non-goals:** `delete` and `recover` in curation; moving Source or Evidence
pages through curation `move` (reclassification relocates them); store-mode
alias tables.

**Load target and budget:** the design of `make-vault-store-canonical`
records the product's load target: 20,000 pages, about 100,000 units and
120,000 edges, with a hub of 10,000 links. The move and create write paths keep
their current latency budget. Every new lookup is an indexed read, batched per
request. No write scans the vault, and the resolver reads lineage from a
derived index, not from page bytes. A move of a hub page rewrites every
referrer in one batch, as `update_wikilinks` does today. A plan may omit
`repoint-links` and leave those referrers to resolve through lineage.

## Decisions

### 1. Lineage lives on the page

Each move of a Markdown page that has a frontmatter block appends
`{path: <old path>}` to `moved_from`, oldest first, through `move_file`'s
`content_transform`, in the same commit as the rename. A reclassification
entry also carries its prior classification keys, as today. `reclassified_from`
stays readable as the legacy spelling and is never rewritten to rename the
key. The page's lineage is the legacy entries followed by the `moved_from`
entries.

The page carries its own history. That works in file mode now and in store
mode later, because a store move already carries new page bytes. A page
without a frontmatter block moves without lineage, as today: Exomem does not
invent a frontmatter block in a page it does not otherwise own.

The two field names are protocol tokens that this specification fixes, so code
holds them with a one-line reason. They are not vocabulary.

An undo is not a move. A reclassification revert and a curation compensation
remove the lineage entry that the forward move appended. An unrestricted
caller's undo appends none and restores every repointed referrer to its exact
bytes. A restricted caller's undo cannot restore a referrer withheld from it,
and it must not reveal that one exists. It therefore always appends an entry
that names the path it vacates, as a move back does, whatever pages link that
path. Its answer and the page's bytes are then the same with and without a
withheld referrer, and a withheld referrer that keeps its repointed link
resolves through lineage, with its pointer-write record still valid. The
choice depends only on the caller's own restriction, which the caller already
knows. A refusal that fired only when a withheld record existed would itself
disclose that record. A plain move back to an old path appends an entry as any
move does.

An undoable move's entry names its operation id and no pointer-write record.
Owner-only state maps the operation id to the record of each page that the
move repointed, and the undo finds them there. Every undoable entry carries
the id, so a reader of the page cannot count the repoints, withheld ones
included. Prior paths still come only from entry paths and the relayout
record.

Lineage also makes every move change the page's bytes. The child batch after
the rename is therefore never empty, and a curation witness always rides in it.

### 2. Resolution follows lineage for reads; write targets stay literal

`_normalize_wikilink` gains two steps. The order becomes: exact path, wrapper-
prefixed path, prior path, retired collection path, stem, title, prior stem.
Slash paths take the path steps and bare names take the name steps, as today.

- A prior path resolves only when exactly one live page that the caller may see
  claims it. The visibility filter runs before claimants are counted. A hidden
  claimant answers as missing.
- A lineage entry equal to the page's current path claims nothing.
- A prior stem resolves only when no live name matches and exactly one page
  claims that stem.
- A→B→C needs no chain walk, because the page at C carries both A and B.
- Graph, audit and inbound-link owners use this resolver, so a derived owner
  never disagrees with a read.

Write targets never follow lineage: the path argument of edit, move, delete or
create names the file it acts on, so the guard and the executor judge the same
path. The page-name step that a command's target passes through follows lineage
only while the dispatcher runs a command declared read-only. A write-capable
command keeps its target literal in every mode, so a `validate_only` or
`dry_run` preview names the page its commit acts on. Outside a dispatched read
the path stays literal. Link resolution inside written content does follow lineage, and the
writer renders a link that names a prior path as a link to the current path.

Lineage has two durable sources. The first is the page's own lineage. The
second is the relayout record that `adopt-root-vault-layout` keeps in the
vault. Each row of that record names an `exomem_id` or none, an old path and a
new path. A bulk relayout writes no `moved_from`, because rewriting every page
would multiply its write cost. A row with an `exomem_id` claims its old path
for the live page that holds that id. A row without one claims it for the live
page whose current path or lineage holds the row's new path. Both sources
follow the same resolution, visibility, reserved-path and squatter-guard
rules. A row that a relayout revert reverses claims nothing.

The lookup reads a derived `prior_paths(prior_path, path)` table beside
`identities`. The writers that refill an identity row refill its prior-path
rows in the same statement, in both the sidecar and the store. The refill
folds in the relayout rows that claim for that page, so each row maps a prior
path straight to its current claimant and a read never walks a chain. The
relayout record is indexed by `exomem_id` and by new path for that refill. The
table is a derived index, not a third source: a rebuild derives it from page
bytes and the relayout record alone.
`find_inbound_wikilinks` also reads the target's own lineage from its bytes,
so delete and move checks never depend on a stale index. The resolver reuses
the per-request batch object of `collection_store.aliases.LinkTargets`.

### 3. Prior paths stay reserved

`unique_path` and every create-only check treat a prior path that a live page
claims as taken. The reservation covers every spelling that names the same
file: case and Unicode form variants of the path. A page may move back to a
path in its own lineage. A holder that the caller may not see answers with
`WRITE_REFUSED`, which names no claimant, path or content, and `unique_path`
picks a different name.

Under path-based scopes, admission refuses a create at a hidden page's current
path before anything is disclosed. A create at its former path refuses in a
vault with the claim and succeeds in a twin vault without it. So the
reservation does disclose something new: that a live page the writer cannot see
once lived at that exact path, or a spelling variant of it, in a folder the
writer can write to. It never says which page. The decision stands, for
integrity: without the reservation, the writer's page would win exact-path
resolution and take the owner's old links.

A file without an `exomem_id` at a reserved path does not take the address.
Following a dead link in an editor creates exactly such an empty file. The
resolver keeps sending the link to the lineage claimant, and the audit flags
the file for removal. A page with its own `exomem_id` at a reserved path does
take the address; the owner's audit reports it (Risks).

### 4. The governed pointer write

The pointer write is the only way an append-only page changes after capture.
The capture writers that finish a page's processing record or assign its
`exomem_id` keep their own rules; they are part of capture.
It has four callers: a move that repoints referrers, the curation
`repoint-links` step, reclassification, and the in-place pointer correction.
Every governed move behaves the same way. Reclassification uses the write for
the Source's own classification fields and folder, and it repoints its
append-only referrers like any move. Its revert restores those referrers from
their pointer-write records. Lineage stays the safety net for every reference
that a move does not write. Each caller declares the spans it intends to
change. The write admits a span only when it is one of these:

- a wikilink target whose old and new forms resolve to the same `exomem_id`;
- the value of a frontmatter field that the `page-fields` registry declares
  with `role: pointer`;
- through a move, the page's own folder and one lineage entry that the move
  appends after every earlier byte of `moved_from`. No declaration admits a
  removal or a `reclassified_from` change; an undo's removal is checked
  against the entry its forward move appended.

A collection value's span ends at its last item's line, so a comment or blank
line after it lies outside every span. A page whose frontmatter uses YAML
anchors or aliases refuses, because an alias lets an admitted change alter
another field's parsed value. A moved append-only page proves its own lineage
change and records it under the move's operation. Only a move, its undo and a
reclassification write `moved_from` or `reclassified_from`; every other writer
refuses a caller-supplied change with `LINEAGE_PROTECTED`, because a forged
entry would claim another page's old path and its links.

The write parses the page and compares the before and after span lists. Every
byte outside an admitted, declared span must be identical. Any other
difference refuses with `APPEND_ONLY`. A regex never decides what changed.
Original media, body text and provenance therefore never change.

A call approves the writes to the page it names: a direct move, promotion or
reclassification needs no digest, and a curation plan approval approves its
in-place corrections. A pointer write to a
page that the call did not name, such as a referrer repoint, needs a curation
plan approval or a commit bound to the digest of the preview that listed it.
The digest proves that the caller saw the side effects. It is not operator
approval, so on the named page it would cost a round trip and prevent nothing.
A missing or stale digest refuses with `POINTER_PREVIEW_REQUIRED`.

Each write creates one create-only, owner-only record. It names the page, the
write's reason, the before and after content hashes, and each span's location,
old bytes and new bytes. An undoable move also writes one owner-only map from
its operation id to its records, by page, before its commit. A curation write keeps that record in its run evidence. A revert applies
only while the page holds the recorded after hash, and its result must hash to
the recorded before hash.

**In-place correction.** A pointer field on an append-only page can be wrong
without the page moving, for example a Source whose `project` names a project
that the owner has merged into another. The correction runs only as a
curation `edit` step with a `patch_frontmatter` operation: the user confirms
the exact plan, and compensation reverts it through the pointer write's
revert, because a superseding correction is refused on a Source. A direct
`edit_memory` `patch_frontmatter` on an append-only page keeps its
`APPEND_ONLY` refusal, which names the curation route for a pointer field.
The correction patches the value span only. It sets no `updated` and keeps
line endings, because `updated` is not a pointer and the span proof would
refuse it.

A pointer field may name a `writer` in the registry, a closed attribute. Such
a field changes on a captured page only through that operation, which checks
what a plain correction cannot: `reclassify` records the classification
history and relocates the source, `custody` checks the raw file's recorded
identity, and `closure` writes the `ingested_into` back-reference when a
compiled page cites the source. The `edit` step refuses such a field with
`POINTER_FIELD_OWNED` and names the operation.

The registry replaces a code list. Because the pointer set defines what may
change on an append-only page, only the shipped pack grants a role: a vault
overlay or a schema save may retire a shipped entry and nothing else, and no
source may declare a key that the write protocol maintains. Widening the set
is a contract change that ships through review. `access._APPEND_ONLY` stays as
old C4 debt for the content refusal; this change does not extend it.

### 5. Moves repoint, and the preview names every outcome

For a page with an `exomem_id`, an append-only referrer no longer refuses the
move. The move repoints it through the pointer write in the same batch. The
preview gives each referrer exactly one outcome:

- `rewritten` for a page the mover may write, including a Records row or a
  Planning item;
- `pointer-repointed (append-only page)`;
- `unchanged: resolves via prior path` for a reference the move does not
  write, such as a link in a page the mover may not write or a prose path in a
  captured page, when the moved page records the move in its lineage;
- `unchanged: keeps the old path` for such a reference when the moved file
  records no lineage, such as a binary file or a page without a frontmatter
  block, so the reference dangles and the owner's audit reports it;
- `refused`, with its cause.

A commit that repoints an append-only page must carry the digest of the
preview that listed it. That is the approval, under the rule of Decision 4.
A page without an `exomem_id` keeps today's `APPEND_ONLY` refusal, because
nothing can prove that a repointed link still names it.

Control costs: the digest prevents an unreviewed side effect on a captured
page that the caller did not name. It fires wrongly only when the preview is
stale, and then the agent previews again at the cost of one call.

### 6. Curation runs `move`, rehomes with `fields`, and repoints once

The relocation predicate narrows to `delete` and `recover`. A curation `move`
step moves bytes and lineage only; it takes no `update_wikilinks`. It takes an
optional `fields` patch, limited to registry pointer fields, so a rehome
changes `project` in the same commit. A `move` still refuses Sources and
Evidence: reclassification relocates them.

A new final step, `repoint-links`, rewrites the referrers of every earlier
`move` step in one batch. It binds a page that an earlier move wrote at that
move's sealed after hash, as the fold `Projection` already does, so a plan of
many moves never hits `CURATION_STEP_CONFLICT`. Between the moves and the
repoint, every link resolves through lineage, so the run has no dangling
window. A plan without `repoint-links` is valid; its referrers keep resolving
through prior paths.

### 7. Recovery adopts a completed rename and never renames

Before the rename, the executor publishes and flushes a create-only intent
record from a pre-rename seam that runs once. The record binds the operation
id, both paths, the sealed before hash and the sealed rename-after state.
Recovery reads the live state:

| State found on recovery | Outcome |
|---|---|
| No intent record; page at the old path with the sealed bytes | Exact retry, if every guard still matches |
| Intent record; page at the old path with the sealed bytes | `CURATION_RENAME_HISTORY_UNPROVABLE`; the owner re-proposes the rest |
| Intent record; old path absent; new path holds the sealed rename-after bytes | Adopt, and roll the batch forward |
| Witness and new path match the sealed postcondition | Committed, through the existing witness recovery |
| Page at the old path with other bytes | `CURATION_BINDING_STALE` |
| Page at neither path, at both, or any other mix | `CURATION_OUTCOME_UNCERTAIN` |

What the strict second row prevents: re-applying a move that someone outside
Exomem undid during the crash window. What it costs when it fires wrongly: one
re-proposal. The fold leaf already decides recovery from live state without a
token, so this is no weakening.

A non-curation move keeps its old crash window between the rename and the
batch. That is old debt and out of scope.

### 8. Adopt publishes the move without a rename

The spike drove a move to a crash right after the rename and then published it
with no rename. In a plain vault, identity, graph nodes and edges, and lexical
search matched an uninterrupted move. In an enrolled vault the catalog served
the stale old path after the crash and the new path after adopt. The adopt
branch reuses most of the move:

- **Semantic preflight:** `preflight_move` must run against a virtual before
  corpus. The disk corpus already holds the page at the new path, so the
  branch puts it back at the old path with `_move_state_map`. The guards bind
  the rename-after state (old path absent, new path holding the sealed bytes),
  not the pre-rename state. Needs an adopt mode.
- **Lifecycle:** `commit_move` rechecks pre-rename guards and cannot be used.
  The branch plans `_plan_move_lifecycle` under the semantic creation lock and
  rechecks the rename-after guards. Needs an adopt commit.
- **Graph:** `prepare_rename_epoch` reads the new bytes from the old path.
  The branch needs an epoch over the same two paths that reads them from the
  new path, then `publish_rename_epoch`.
- **Reused unchanged:** `prepare_catalog_membership_batch` with the old path
  removal bound to the sealed before hash, `publish_markdown_batch`,
  `batch_atomic_write` with the destination bound to the sealed bytes, and
  `index_sync.delete_after_remove` for the old path.
- **Not yet exercised:** the curation witness in the batch, store mode,
  paired binaries and part folders, the activity log entry, working-set heat,
  watcher suppression and restricted movers.

In store mode the move commits in one store transaction, so there is no
rename window and recovery reads committed or not committed from the store.

### 9. Compensation restores sealed bytes

Compensating a `move` moves the page back with its sealed pre-step bytes:
the lineage entry and the `fields` patch go away. Compensating `repoint-links`
returns each rewritten page to its sealed pre-step bytes, and each
append-only page through the pointer write's revert. Compensating an `edit`
step that corrected a pointer field on an append-only page also uses that
revert. Compensation runs in reverse order, so referrers return before their
target does. The proposal judges each inverse move's contract from the state
that the compensation's earlier steps leave: by then a referrer names the
page's old path again.

## Risks / Trade-offs

- A stale `prior_paths` row can miss a claim after an outside edit. The link
  then reads as unresolved, never as another page, and the next index refresh
  repairs it. Inbound and delete checks read lineage from bytes.
- Reserving prior paths can refuse a create that the owner meant as reuse. The
  refusal names the claimant when the caller may see it, and moving the
  claimant releases nothing: the owner renames the new page instead.
- Lineage grows with each move. Entries are short paths, and pages rarely move
  more than a few times.
- A create checks the reservation before its commit, not inside it, in file
  and store mode. The commit cannot tell that create from importer, recovery,
  init and log writes that also expect an absent path, so a recheck there would
  refuse writers that claim no reservation. A create that races a move onto
  the move's old path can therefore commit within that one-write window, and a
  sync or an outside editor can bring in a page with its own `exomem_id` at a
  claimed path. Such a page wins exact-path resolution, so the claimant's old
  links resolve to it with no broken link to show it. The audit reports it to
  the owner as `reserved_path_identified`, and the owner repoints the old links
  or moves the page to its own name.
- A restricted caller's revert leaves the Source with a lineage entry for the
  path it vacated, so that path stays reserved and a later revert finds no
  classification to restore. The owner reclassifies explicitly instead.

## Coordination

`make-source-reclassification-reversible` shipped Phase 1 with
`reclassified_from`. Its delta now names `moved_from` and records this change
as the delivery of its Phase 2, whose history-aware reads this change widens
from Sources to every page with lineage. Its path-link scenario changes from a
refusal to a repoint through the pointer write, and its revert restores the
repointed pages from their records. That change archives after this one ships.
This change does not modify its reclassification requirements.

`adopt-root-vault-layout` keeps the relayout record in the vault as the second
lineage source, with the row shape above. Its `command-surface` delta of
"Append-Only Tree Relocation", which allowed link-only repoints in store mode,
moves into this change. This change's version is the single rule for both
modes, so that change drops its delta and archives after this one. The
relayout's link and pointer-field edits in append-only pages run through the
pointer write, approved by the digest of the relayout plan.

## Open Questions

- Pages without an `exomem_id` keep the `APPEND_ONLY` refusal. Older vaults may
  hold many such pages; a census on a real vault copy would size the gap.
