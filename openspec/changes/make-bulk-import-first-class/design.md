## Context

`make-collection-adoption-lossless` (archived 2026-10-25) made Adoption Studio import every file of a collection with its bytes, links, tags, homes and re-run identity. Every item still lands as a Source. This change adds the two steps that make a bulk import useful afterwards: a person's own notes become notes, and the uncompiled result reaches the agent as work it can finish.

The existing substrate is reused, not replaced:

- custody, classification, plan identity and bounded apply: `adoption_run.py`, `adoption_plan.py`, `adopt.py`;
- note creation, source citations and back-references: the `note` leaf, `source_closure.py`;
- outstanding work: `audit.py` families, `attention.py`, the maintained due-state projection in `due_state.py`, the bootstrap and write-advisory renderers;
- staging: the Cloud layout `_Imports/<import_id>/` that `adoption_studio start` already scans.

## Goals / Non-Goals

**Goals**

- An agent with file access imports a folder with one command and one plan confirmation; an upload reaches the same engine.
- Own notes become draft notes with their originals kept as Sources; documents and exports stay Sources.
- The audit reports a Source as compiled only when a live note compiles it.
- Outstanding Sources and the existing due families reach the agent in bounded batches that it can act on, with counts that only fall when the work is done.

**Non-Goals**

- No server-side model, classifier or topic inference. The agent judges; the server measures and validates.
- No new queue engine, background worker or notification service.
- No batching redesign of enrichment. That waits for the enrichment measurement and its own plan.
- No change to the Cloud upload transport; the Home page work is a separate Substrate change.
- No automatic promotion of a draft note to live.

## Decisions

### D1. The agent classifies; the registry validates

A `select` folder rule gains `adopt_as`, with values `source` (the default and today's behaviour) and `note`. A `note` rule names a `note_type` that the note-type registry holds, and may name a `placement` that the registry or the placement owner admits for that type. `select` refuses an unregistered note type or placement with no selection change, as it refuses an unregistered `source_kind` today. The rule is part of the selection identity, so `plan_id` changes when the classification changes.

The scan already reports deterministic measurements per item (extension, size, front matter present, link counts). The agent combines them with the folder layout and, where it needs to, by reading a sample. The server never decides that a file "looks like" a note: that would be a word list or heuristic over meaning (registry C6).

Alternative rejected: a per-file `adopt_as` override list as the only mechanism. Folder rules match how collections are organised; per-path overrides already exist for exceptions and keep working.

### D2. An own note is a draft note plus its lossless Source

For an own-note item, `plan` previews two outputs: the Source copy, exactly as a `source` item, and a note target in the note type's home. The note body is the original note body after its front matter, unchanged, followed by the generated `## Links` section that a Source carries (the import never rewrites a sentence of the original); its title, tags and aliases come from the same parsed front matter that the Source already carries. The note's `sources:` cites the Source copy. Links from one own note to another resolve to the planned note, not to the Source; links to non-note items resolve to their Sources, as today.

The note's status is `draft`. Live creation needs a typed relation or a reviewed disposition (the semantic write contract refuses `active` with `RELATION_DISPOSITION_MISSING`), and an import has reviewed nothing. Recording `reviewed_none` for a thousand unread notes would be a false review. A draft is the honest state, and D4 keeps its Source visible as work.

Each item's outcome records the note's stable reference with the Source's. A retry, a crashed apply's recovery and a re-run find the note through that record and the collection identity, and report `already-applied`. An occupied note target fails the item with `TARGET_OCCUPIED`, as for Sources. A changed original on re-import creates a new Source that names the earlier import; the existing note is not edited, and the new Source is outstanding work that cites the earlier note as context.

The note goes through the governed note-creation path, so the semantic write contract, source closure and path confinement stay authoritative. Its destination comes from that path's existing rules for the note type (with `project` or `domain` where the type needs one). This change adds no placement logic; when registry-backed placement lands in the note leaf, own-note adoption inherits it.

The note records the same import identity as its Source (`import_collection`, `collection_path`), so the identity check that finds earlier Sources also finds earlier notes.

### D2a. The note is its own governed step after its Source commits

Source closure reads each cited Source from disk, so a note cannot join the atomic batch that creates its Source. `apply` therefore commits a chunk of Sources as today, then creates the chunk's draft notes one by one through the two-phase governed creation (validate, then commit), as `adoption_proposals` already does for compilation proposals. Each note commit writes the note and its Source back-reference atomically. The run object records each note's outcome with its commit.

An own-note item is complete only when both its Source and its note exist. A budget stop can fall between two notes; the continuation resumes with the next note. After a crash, the identity check finds a note that committed before its outcome was recorded and records it instead of creating a second one. The phase becomes `applied` only when every item is complete.

Alternative rejected: making the note leaf return planned writes for the chunk batch. It would need closure to resolve Sources that are not yet on disk, which reopens the custody checks that the lossless change settled.

### D3. One plan confirmation covers the import

The adoption `apply` already binds its writes to the exact previewed `plan_id`, and clients gate it as a destructive call. Own-note creation is part of that plan. It only creates pages that did not exist and never edits, moves or deletes existing pages, so it adds no authority beyond what the plan shows. The person confirms one plan for the whole collection, never one prompt per file.

Curation keeps its exact-plan confirmation and one-step-per-call execution for restructuring: edits, moves, merges, deletes and supersessions. Promoting a draft to live, or compiling a Source, goes through the existing writers, each with its own guards.

Alternative rejected: running own-note creation through curation plans. Curation executes one step per call; a 1,000-note collection would need 1,000 calls and gains no safety, because creation is additive and the adoption plan already binds it.

### D4. A Source is compiled only when a live note compiles it

`ingested_into` remains a citation back-reference and keeps being written for every citing page, so the graph and the citation views do not change. It is no longer a completion claim.

`unprocessed_source` uses one coverage predicate. A Source is compiled when at least one page named by its `ingested_into`:

1. exists and is a compiled page (not a Source);
2. has a status whose registry class is `live`;
3. cites the Source in its own `sources:` claim, resolved through `source_closure`.

Otherwise the Source stays in the family. Its finding reports each derivative that fails the predicate with the reason (`pending`, `retired`, `missing`, `claim_unresolved`), so the agent sees the next action: promote the draft, compile the Source, or archive it. The predicate branches only on the registry's closed `class` attribute, never on a status key.

The finding's fingerprint covers the Source's content hash and each derivative's reference, status class and claim result. A derivative that moves from draft to live, loses its claim or is deleted changes the fingerprint, so an earlier dismissal does not hide the changed state.

The audit selects Sources by the note-type registry's `source` role, not by the literal `type: source` key it checks today.

### D5. The backlog reaches the agent through the due-state projection

Today `unprocessed_source` is computed by a whole-vault audit on each attention call and never enters the maintained due-state projection, so bootstrap and post-write responses never mention it. Bootstrap's compact core shows only a total, the largest category and a pointer to a section, with no way to act.

- `unprocessed_source` joins the projection's categories. Its due date is the Source's capture date. Its predicate is page-local over the cached pages, so a write can refresh it.
- A governed write that creates, edits, supersedes or deletes a page refreshes the projection rows of every Source that the page cites before or after the write. The source-closure plan already names those Sources. Out-of-band edits are healed by the existing reconcile.
- The served block keeps its shape and adds the objective count. `total` and `categories` stay the counts the audience sees after review state; `outstanding_total` and `outstanding` count the same rows before dismissal and snoozing. Dismissing items lowers what is shown, never what is outstanding.
- Bootstrap's compact core reports the per-category counts and one `next` route: `review_memory(mode="attention", categories=[<largest category>], limit=<batch>)`. The route is generic over categories, so no per-category table enters code.
- The post-write block's category bound follows the projection's category list instead of its own constant, so adding a category cannot silently drop a block.

### D6. The bounded batch uses existing review modes

No new tool, mode or input parameter. The loop is:

1. `review_memory(mode="unprocessed-sources", limit=N)` (or `mode="attention"` with the category) returns at most N outstanding Sources. Each finding carries the Source's title, a bounded excerpt, its recorded grouping metadata (`import_collection`, the collection folder, `source_kind`, `domain`, capture date) and each derivative with its state (D4).
2. The agent groups the batch by topic, from the excerpts and metadata. The server proposes no groups.
3. For each group, `review_memory(mode="compilation", sources=[...])` returns the existing scaffold plus a bounded excerpt of each Source, so the agent can write the note without one read per Source.
4. The agent writes through the existing writers: a new note with `sources:`, a governed edit that promotes an own-note draft to live with its relations, or an archive of a Source that needs no note.
5. The next refresh shows the count fall only for Sources that a live note now compiles.

The scaffolded skill teaches this loop for imported collections and for the other due families, and says that dismissal is not completion.

`unreflected_observations` keeps its existing rule: it clears only through a declared collection reflection, never through a note. This change does not touch the collection writers.

Alternative rejected: a new bulk work-item tool. Curation's work item refuses Source targets, the adoption work item is bound to one run, and the tool surface is under a frozen schema evaluation; the existing modes already take the inputs the loop needs.

### D7. A folder outside the vault imports through local staging

`adoption_studio start` keeps its vault-relative contract. A path that is absolute, or that climbs out of the vault by `..`, is refused with `IMPORT_PATH_OUTSIDE_VAULT`, whose message names `exomem import <folder>`. The check reads the path as text and never resolves it on the filesystem, so a network-share spelling cannot make a Windows host connect to the host it names. A path that escapes through a symbolic link inside the vault gets the overview resolver's `INVALID_PATH`.

`exomem import <folder>` is a local CLI command. It enumerates the folder without following symbolic links and applies the selection rules below before it reports or copies anything. It then reports the selected file count and bytes against the free space of the vault's filesystem plus a reserve, and refuses before copying when the copy does not fit. The limits that the adoption engine applies after staging are warnings, not refusals, because a smaller copy cannot fix them: the files listed per `start` (the report then says to start on subfolders and to pass `collection="<folder name>"` so they stay one collection), the per-file size ceiling, and the second copy that adoption makes. Otherwise it copies each selected regular file into `_Imports/<import_id>/<folder name>/`, streaming, with a manifest of relative path, size and SHA-256, then re-hashes the copies. It reports progress while it copies. The original folder is never modified, and the folder name is the collection label.

The selection rules are shared with `exomem cloud import`, so one folder gives the same selection on either route:

- A hidden entry (a name that starts with a dot, or the Windows hidden attribute, at any depth) is left behind by default.
- The `.gitignore` files inside the selected folder apply by default, read through a maintained gitignore parser, with git's precedence: the deepest file with an opinion decides, and an ignored folder is not entered. Folders and files are decided separately, because a catch-all pattern followed by a folder re-include keeps the files below the folder in git. The command never hard-codes a folder name such as `node_modules`; the folder's own rules say what to leave.
- A symbolic link or a Windows junction at any depth is left behind and never followed. A special file or an unreadable file is left behind with its own reason.
- Before it copies or encrypts, the command reports what is left behind: count and bytes under each reason (hidden, gitignore, symbolic link, special file, unreadable), each entry under the first reason that applies. `--include-hidden` takes hidden entries, and `--no-gitignore` ignores the `.gitignore` files.
- Every entry left behind gets a per-path code in the manifest; none is dropped silently.

A file that changed while it was read, could not be read, or was refused by the disk (for example a full disk) is left out with a code, and the import stays open: the command exits with an error and says that the same command run again retries only those files under the same import id. A file that the vault refuses because of its own name or path (a name the vault never accepts, or a case collision) is final: the manifest records it as left out with the vault's code, the import finishes without it, and the report gives the count. An interrupted or incomplete import resumes under that id without copying a verified file again. On a resume, a staged file that the folder no longer selects is removed and recorded in the manifest, and a staged file that changed after it was verified is copied again. An unfinished import made with other options is named with its staged folder and is not resumed. One lock file per import stops two runs from writing one manifest, and a torn last manifest line is cut off before the next line is added.

When the import finishes, the command ends with the `adoption_studio start` path, and with the bytes it staged. An import that stays open also prints that path, so the user can adopt what is staged so far before the rerun. It states that the staged folder is a second copy of the data on the disk until it is discarded. No Exomem command discards a staged folder: after adoption finishes, the user deletes `_Imports/<import_id>/`. The Sources that adoption committed are governed vault pages and stay.

The manifest lives in the user's state folder, not in the vault: a state folder created next to a vault that is not yet initialised blocks the later initialisation.

This reuses the Cloud staging layout, so `_collection_of`, collection identity and re-run dedupe work unchanged. The adoption-studio requirement that local runs never use staging changes to: local runs scan in place inside the vault, and stage only a folder outside it.

Alternative rejected: letting `start` read an outside path on a local install. It widens root confinement for every caller of the server to serve one command-line case.

### D8. The Cloud CLI needs no setup and reports its phases

`exomem cloud import` and `exomem cloud export` default `--mcp-url` and `--api-base-url` to the production Cloud endpoints. The flags and `EXOMEM_CLOUD_MCP_URL`/`EXOMEM_CLOUD_API_BASE_URL` still override them. The endpoints are one product constant in one module.

`exomem cloud import` selects files with the rules of D7, so a symbolic link is skipped and reported instead of stopping the archive walk, and it reports what it leaves behind before it builds the archive. Its preflight then reports the selected files and bytes, and refuses with the measured values when `age` is not on PATH or when the checkpoint directory's free space is smaller than the ciphertext it will write plus the same reserve as local import (the ciphertext is a full-size copy, and the estimate counts the tar headers of every member). It also counts the archive's members, folders included, and refuses before it encrypts, with the measured count and the limit, when the count exceeds the cell's member limit. The client imports that limit from the cell's import module and never copies its value; the cell enforces the path and name limits.

It prints human-readable progress to standard error for archive and encryption (bytes written), upload (parts and bytes), and server import (state). Every failure names its phase, a content-free reason code and the import id, and never a file name or a presigned address. The final standard-output line stays one JSON object, and fields are only added. The existing checkpoint keeps resume, except that a checkpoint that cannot resume is removed and the failure says so: a file that changes while it is archived fails the run with `SOURCE_CHANGED`, and the message says that a rerun starts a new import and names the routes that stop the open Cloud import: Home's Cancel, and the `import_cancel` action, which an older cell may lack. A registration that Cloud refuses because another import is open (the cell allows one) keeps the checkpoint and does not say to resume: it says to cancel the open import in Home or wait for it to expire, and then to run the command again. When the run ends, it reports the ciphertext bytes that stay on the disk, states that they duplicate the data until the import finishes, and names the discard route: delete the checkpoint folder and ask the assistant to cancel or discard the import through `transfer_memory`.

### D9. Media apply uses existing durable derived batches

Media import already commits its sidecar and queues extraction. Its post-terminal fan-out can still join graph work before the reply returns. `adoption_studio(action="apply")` uses the existing durable acknowledgement session when `EXOMEM_FAST_DURABLE_ACK` is absent. This removes that join without another queue, worker or custody scope.

The existing receipts bind pending visibility and replay to the committed bytes. Governance commits stay synchronous. Before note preflight, the importer registers its committed Source in the writer resolver so that the same call can cite it. The note leaf still checks that Source's custody and authorization. The existing drain owns graph and index convergence. Extraction can remain pending or blocked, and the response reports that state without claiming completion.

The flag retains its global behavior: `1` enables eligible writes, and every explicit value other than `1` restores synchronous fan-out. Other mutations keep their current default. Bulk apply retains its own request budget for canonical writes; this change makes no two-second bulk-call claim.

Chunk and individual-note pacing retain the existing terminal reserve for mandatory custody proof and pending publication. An expired acknowledgement budget returns pending state without scanning every component. It retains the stable advisory reference when one applies. Operational checkpoint writes leave existing graph debt with its durable owner while the acknowledgement session is active. The explicit synchronous rollback can still drain that debt.

The boundary proof blocks sidecar fan-out and checks that default apply returns with bytes, a sidecar, an extraction job and pending recall intact. It also checks exact replay, convergence and the explicit synchronous rollback. The own-note benchmark repeats the existing seed with caller-supplied selection rules and checks every page after the rerun.

## Risks / Trade-offs

- **Existing backlogs grow.** Sources whose only derivatives are drafts, or whose back-reference is stale, reappear in `unprocessed_source`. This is the honest count; the release notes say so.
- **Duplicate text on disk.** An own note's text lives in the note and in its Source. That is the cost of lossless custody plus an editable note.
- **Drafts in recall.** Draft notes are retrievable like other drafts. Recall's existing preference for live pages applies.
- **Agent misclassification.** A wrong `adopt_as` rule creates draft notes for documents. The plan preview shows the targets before apply, and a draft can be superseded or archived through existing writers; the Source keeps the bytes either way.

## Migration Plan

No data migration. The coverage predicate is computed from current pages on each audit and projection refresh. Rollback is a release rollback; draft notes created by an import stay valid pages.
