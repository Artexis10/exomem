# Write Scope

The single most important rule of this skill: **what can and cannot be written
to.** Everything else is downstream.

## Binary placement decision tree

Three locations hold binary files in the KB. Pick by **origin and purpose**:

```
Is the artifact something you reason from (input to your thinking)?
  │
  ├─ Yes → Sources/<type>/_attachments/
  │         (a markdown source-note in Sources/<type>/ captures the analytical surface;
  │          the binary lives alongside as the original reference)
  │
  └─ No → was the artifact produced by your own compiled note?
           │
           ├─ Yes → Notes/<type>/<scope>/_attachments/
           │         (output of an experiment, research-note, or production-log;
           │          lives near the parent note that produced it)
           │
           └─ No → the artifact came from a third party and must be preserved as-received
                  → Evidence/<scope>/<category>/
                    (official letters, signed contracts, third-party documents;
                     append-only, no analytical processing, no frontmatter)
```

**Worked examples:**

- An article or PDF you found that informs a project → `Sources/Articles/_attachments/` with a markdown capture in `Sources/Articles/`. You reason from it.
- A protocol or document you authored and shared → `Notes/<...>/_attachments/`. Your work produced it.
- An official document received from a third party → `Evidence/<scope>/`. Third party, preserve as-received.
- A Sources/Sessions transcript of a conversation → not a binary case; lives as markdown in `Sources/Sessions/` directly.

**Why this matters:** mixing layers dilutes their epistemic discipline. Evidence
binaries reasoned over become "sources we lightly analysed," losing the
as-received guarantee. Keep the layers honest and the categories stay useful.

**How the bytes get there (never base64 through the model):** the tree above is
*where* a binary lands; delivery is a separate question of *how*. Encoding a file
into a tool argument is billed as model output tokens, so always deliver
out-of-band — the upload endpoint (or the prefilled upload page when you're on
claude.ai web), an Obsidian Sync drop, or a direct disk write from Claude Code.
Full detail: `references/operations.md` § preserve.

## Writeable paths (Knowledge Base only)

The skill may write to anything inside `<vault>/Knowledge Base/`, subject to
per-operation rules:

| Path | Operations | Notes |
|---|---|---|
| `Knowledge Base/Sources/**` | add | Append-only; never edit existing files. `_attachments/` subfolders may hold binary originals (append-only). |
| `Knowledge Base/Notes/**` | note, replace | Propose-then-confirm by default. `_attachments/` subfolders may hold binary outputs (append-only). |
| `Knowledge Base/Entities/**` | link, replace | Propose-then-confirm by default |
| `Knowledge Base/Evidence/**` | preserve | Append-only store for third-party artifacts; never edit existing files; no frontmatter required on binaries |
| `Knowledge Base/index.md` | any write | Catalog of what exists; auto-updated as part of every confirmed write |
| `Knowledge Base/log.md` | any write | Chronological activity log; appended (never edited) on every confirmed write |
| `Knowledge Base/<subfolder>/index.md` | any write | Catalog only; no orientation prose (orientation lives in `_Schema/references/`) |
| `Knowledge Base/_Schema/**` | manual only | Schema is human-edited; the skill does not modify itself |

## Read-only paths (rest of the vault)

The skill **reads from** any folders in the vault outside `Knowledge Base/` to
inform compiled notes (links, citations, context). It **never writes to** them.
Examples of folders you might keep alongside the KB:

- Hand-authored reference or strategy material (`Reference/`, `Strategy/`, …)
- Personal trackers, datasets, or admin folders
- Any other top-level folder in the vault that is not `Knowledge Base/`

If you explicitly ask the skill to write to one of these paths, it **declines and
explains** that those are read-only inputs, and offers to compile the finding into
a `Notes/Research/<scope>/` page that links back instead.

The single exception: an explicit, unambiguous override in the conversation
("override write-scope, edit that file directly"). The skill can then comply but
must (1) show the proposed diff first, (2) note that this bypasses the standard
rule, and (3) wait for an explicit second confirmation. The rule prevents
accidental modification, not deliberate human intent.

## Why hand-authored paths are read-only

LLM-compiled material has different epistemic status from hand-authored material.
Mixing them blurs which claims you stand behind vs. which the model produced. The
Knowledge Base exists as a separate layer precisely so this distinction stays
sharp. Compiled research can and should **link** into hand-authored material —
that's how the layers connect — but should never **modify** it.

## Sources are append-only

Within `Knowledge Base/Sources/`, files are never edited after creation. If a
source needs correction:

- For factual error: capture a new source, supersede the compiled notes that drew on the old one.
- For typo or formatting: leave it. Sources reflect what was captured at capture-time.

The append-only rule governs **content, not location**. Relocating a source
*within* `Sources/` — e.g. into a themed sub-folder — is allowed via `move_file`:
the bytes are unchanged, only the path moves, and inbound wikilinks are rewritten.
Moving a file *out* of `Sources/` (or *into* it from elsewhere) remains forbidden.

## Index files are skill-managed

`index.md` files (top-level and per-subfolder) are written by the skill on every
confirmed write. They are **catalogs**, not curated docs — don't hand-edit them.
If audit detects index drift, it proposes a regeneration.

The split between `index.md` (catalog) and `log.md` (chronological activity feed)
is deliberate. Subfolder `index.md` files are catalog-only: a header, a brief
one-line statement of what the folder holds, then the entry list. Orientation
prose belongs in the schema, not in indexes.

The skill never writes to `_Schema/SKILL.md` or anything in `_Schema/references/`.
The schema evolves through human edit. Self-modification is out of scope.
