# Write scope

The most important rule of this skill is what can and cannot be written to. Everything else follows from it.

## Memory boundary

Keep temporary working context in the current conversation. Evermatter holds durable preferences, feedback,
personal baselines, project context, decisions, failures, experiments and proof-bearing records, subject to
the live capture policy. Custom instructions can guide one client, but they do not replace this durable home.
Use native memory only as an explicit fallback when Evermatter cannot capture, and move durable results to
Evermatter when capture becomes available.

Do not turn transient scratch, reminders, tasks, credentials or every passing chat remark into Knowledge Base
material. Capture only raw material worth preserving, or a compiled conclusion that future agents should be
able to retrieve.

## Where a binary file goes

Three locations hold binary files. Pick by origin and purpose, and let MIME play no part. Ask in order:

1. Is it something you reason from, an input to your thinking? It goes to `Sources/<type>/_attachments/`, with
   a markdown source note in `Sources/<type>/` as the analytical surface.
2. Is it a file you generated and handed the user, or a proof-bearing output? It goes to
   `Evidence/<scope>/<category>/`, as the exact bytes you handed over. A draft you never hand over stays
   ephemeral.
3. Did your own compiled note produce it, as supporting output that you never handed over? It goes to
   `Notes/<type>/<scope>/_attachments/`, beside that note.
4. Otherwise it is a third-party artifact kept as received. It goes to `Evidence/<scope>/<category>/`:
   append-only, with no analytical processing and no frontmatter.

Examples:

- An article or PDF that informs a project: `Sources/Articles/_attachments/`, with a markdown capture in `Sources/Articles/`.
- A social image, report or export you hand the user: `Evidence/<scope>/<category>/`.
- An official document received from a third party: `Evidence/<scope>/`.
- A conversation transcript is not a binary case. It lives as markdown in `Sources/Sessions/`.

Mixing layers dilutes their epistemic discipline. Evidence holds exact proof-bearing or handed-over bytes, and
Sources hold material reasoned from. Keep the layers honest and the categories stay useful.

Never encode a file into a tool argument: the encoding bills as model output. Pass a file handle in `files` of
`save_source` or `save_evidence`, or hold the bytes with `upload_files` ([tools](tools.md)). Never write into
`Sources/` or `Evidence/` straight to disk: the governed route records custody.

## Writeable paths (Knowledge Base only)

The skill may write inside `<vault>/Knowledge Base/`, subject to per-tool rules.

| Path | Written through | Notes |
|---|---|---|
| `Knowledge Base/Sources/**` | `save_source` | Append-only; never edit an existing file. `_attachments/` subfolders may hold binary originals, also append-only |
| `Knowledge Base/Notes/**` | `create_note`, `edit_note`, `add_observation`, `supersede_note` | Follow the live engagement policy, the operation envelope and the standing authority. `_attachments/` subfolders may hold binary outputs, append-only |
| `Knowledge Base/Entities/**` | `connections.create_entity`, `edit_note`, `supersede_note` | Same policy as Notes |
| `Knowledge Base/Evidence/**` | `save_evidence` | Append-only proof-bearing and handed-over artifacts; no frontmatter on binaries |
| `Knowledge Base/index.md`, `<subfolder>/index.md`, `log.md` | the server | Catalogs and the activity feed. The server updates them on every governed write. Never hand-edit them |
| `Knowledge Base/_Schema/**` | governed schema and registry writers (`schema.*`) | Follow the operation envelope and any active holds. An instruction change needs an explicit user request; never rewrite the skill autonomously |

A subfolder `index.md` is a catalog only: a header, one line on what the folder holds, then the entries.
Orientation prose belongs in the schema. If audit finds index drift, it proposes a regeneration.

## A legacy page `add_observation` refuses

`add_observation` changes a writable compiled page only. A page with no compiled `type`, or no frontmatter at
all, is refused with a remediation that names the existing edit owner. Use that authorized owner for an
in-place correction, and verify the current page and the retained interpretation. If the surface lacks that
owner, report the unavailable operation and keep the intended correction for a capable surface. Do not create a
dated child. Migrate a legacy page into a compiled type only on request, since nothing does it automatically.

## Read-only paths (rest of the vault)

The skill reads folders in the vault outside `Knowledge Base/` to inform compiled notes (links, citations,
context). It never writes to them. They might hold hand-authored reference or strategy material, personal
trackers, datasets or admin folders, and any other top-level folder.

If you ask the skill to write to one of these paths, it declines, explains that they are read-only inputs, and
offers to compile the finding into a `Notes/Research/<scope>/` page that links back.

The single exception is an explicit, unambiguous override in the conversation, such as "override write-scope,
edit that file directly". The skill then complies, but it must (1) show the proposed diff first, (2) say that
this bypasses the standard rule, and (3) wait for an explicit second confirmation. The rule prevents accidental
modification, not deliberate human intent.

LLM-compiled material has different epistemic status from hand-authored material. Mixing them blurs which claims
you stand behind and which the model produced. Compiled research should link into hand-authored material, and
should never modify it.

## Per-subtree access overrides (`_access.yaml`)

The location rules above are the defaults. To carve out exceptions, add an optional `Knowledge Base/_access.yaml`.
An exception is a folder inside `Knowledge Base/` that the skill must never write, a read-only sibling outside it,
or a private subtree that should not even be searchable.

```yaml
# Both lists are optional. Each entry is a subtree prefix: a top-level sibling folder,
# or a path relative to the Knowledge Base (a leading "Knowledge Base/" is ignored).
readonly:          # searchable and indexed, but every write is refused
  - Reference
  - Notes/Research/Frozen Report
excluded:          # hidden from search and embeddings, and unwritable
  - Personal/Journal
```

- **`readonly`**: pages stay searchable and readable, and any write is a hard refusal with no conversational
  override. Use it to fold a curated folder into the Knowledge Base without exposing it to edits.
- **`excluded`**: the subtree leaves search and the embedding index, and it is write-protected. Use it for
  genuinely private material.
- The server reloads the file on the next call, so no restart is needed.
- `init` does not ship the file. Without it, only `Sources/` and `Evidence/` (append-only) differ from
  ordinary read-write. `exomem personalize` (CLI only) generates a starter policy from a scan of the vault.
- Resolution order for any path: `excluded`, then `readonly`, then append-only (`Sources/`, `Evidence/`), then
  read-write.

## Sources are append-only

Within `Knowledge Base/Sources/`, files are never edited after creation. If a source needs correction:

- For a factual error, capture a new source and supersede the compiled notes that drew on the old one.
- For a typo or formatting, leave it. Sources reflect what was captured at capture time.
- For a wrong `source_type` or `domain`, correct it with
  `manage_memory(operation="files.reclassify", arguments={'path': ..., 'source_kind': ..., 'domain': ..., 'reason': ...})`.
  Preview it first with `inspect_memory(operation="files.reclassify")`: it shows the destination, the references
  it rewrites and any refusal.
- To undo the latest correction, call
  `manage_memory(operation="files.revert_reclassification", arguments={'path': ..., 'reason': ...})`.

The append-only rule governs content, not location, and not the capture-time classification.

- Moving a source within `Sources/`, for example into a themed subfolder, is allowed through
  `manage_memory(operation="files.move", arguments={...})`. The bytes stay unchanged and inbound wikilinks are
  rewritten.
- Reclassification is the governed form of the same move. It derives the destination from the corrected
  classification, requires a reason, appends the previous path, kind and domain to `reclassified_from`, and
  leaves the body byte-identical.
- A link that names the source by bare name keeps resolving after the move. A path link in an append-only page
  would dangle, so the move is refused.
- Revert restores the latest recorded path, kind and domain. It refuses an entry that records only a path.
- Moving a file out of `Sources/`, or into it from elsewhere, stays forbidden.

The skill never writes to `_Schema/SKILL.md` or to `_Schema/references/`. The schema evolves through human edit.
