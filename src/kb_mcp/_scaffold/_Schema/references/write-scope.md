# Write Scope

The single most important rule of this skill: **what can and cannot be written to.** Everything else is downstream.

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
                    (legal letters, medical team documents, signed contracts;
                     append-only, no analytical processing, no frontmatter)
```

**Worked examples:**

- A PDF you found about EGCG dosing that informs the cancer experiment → `Sources/Articles/_attachments/` with a markdown capture in `Sources/Articles/`. You reason from it.
- The clinical protocol document you authored and shared with the oncology team → `Notes/Experiments/Health/_attachments/`. Your experiment produced it.
- The Estonian consilium document received from the medical board → `Evidence/Mother Cancer/01 - Clinical Documentation/`. Third party, preserve as-received.
- A formal warning letter from your employer → `Evidence/Yolo/01 - Warning Letter .../`. Third party, preserve as-received.
- A Sources/Sessions transcript of a Claude conversation → not a binary case; lives as markdown in `Sources/Sessions/` directly.

**Why this matters:** mixing layers dilutes their epistemic discipline. Evidence binaries reasoned over become "sources we lightly analysed," losing the as-received guarantee. Source binaries treated as outputs lose the audit trail of what you read. Note outputs dropped in Evidence pretend to come from outside when they came from you. Keep the layers honest and the categories stay useful.

## Writeable paths (Knowledge Base only)

The skill may write to anything inside `<vault>/Knowledge Base/`, subject to per-operation rules:

| Path | Operations | Notes |
|---|---|---|
| `Knowledge Base/Sources/**` | add | Append-only; never edit existing files. `_attachments/` subfolders may hold binary originals (append-only). |
| `Knowledge Base/Notes/**` | note, replace | Propose-then-confirm by default. `_attachments/` subfolders may hold binary outputs (append-only). |
| `Knowledge Base/Entities/**` | link, replace | Propose-then-confirm by default |
| `Knowledge Base/Evidence/**` | add | Append-only binary store for third-party artifacts; never edit existing files; no frontmatter required on binaries |
| `Knowledge Base/index.md` | any write | Catalog of what exists; auto-regenerated/updated as part of every confirmed write |
| `Knowledge Base/log.md` | any write | Chronological activity log; appended (never edited) on every confirmed write |
| `Knowledge Base/<subfolder>/index.md` | any write | Catalog only; no orientation prose (orientation lives in `_Schema/references/`) |
| `Knowledge Base/_Schema/**` | manual only | Schema is human-edited; skill does not modify itself |

## Read-only paths (rest of the vault)

The skill **reads from** these paths to inform compiled notes (links, citations, context). The skill **never writes to** these paths under any circumstance.

- `Cognitive Core (Timeless)/**`
- `Systems Thinking/Domains/**`
- `Systems Thinking/AI Collaboration/**` (especially `Prompt Bank/Primitives/` and `Prompt Bank/Masters/`)
- `Systems Thinking/Decision Frameworks/**`
- `Systems Thinking/Mental Models/**`
- `Personal Context (Evolving)/**`
- `Products/<X>/Strategy.md`
- `Products/<X>/Vision and Economics.md`
- `Products/<X>/Roadmap.md`
- `Products/<X>/Triggers.md`
- `Products/<X>/Expansion Surfaces.md`
- `Products/<X>/Agentic RAG.md` (and similar hand-authored product files)
- `Primitives/**`
- `Domains/**` (if present at vault root)
- Any other top-level folder in the vault that is not `Knowledge Base/`

If the user explicitly asks the skill to write to one of these paths (e.g., "update Q's Strategy.md with this finding"), the skill **declines and explains** that those are read-only inputs. The skill can offer to compile the finding into a `Notes/Research/Q/` page that links back to `[[Products/Q/Strategy]]` instead.

The single exception: if Hugo issues an explicit, unambiguous override in the conversation ("override write-scope, edit Strategy.md directly"), the skill can comply but must:
1. Show the proposed diff first
2. Note that this bypasses the standard write-scope rule
3. Wait for an explicit second confirmation

This exception exists because the rule is meant to prevent accidental modification, not to be a hard wall against deliberate human intent.

## Why curated paths are read-only

The vault has a tier structure that predates the Knowledge Base:

- **Cognitive Core, Domains, Prompt Bank/Primitives** — Hugo's authored governance layer. These are slow-moving, deliberately edited, and have outsized influence on every downstream operation.
- **Products/<X> top-level files** — Hugo's product strategy thinking. These are his own synthesis, not LLM-compiled material.
- **Personal Context (Evolving)** — personal profile, voice, calibration. Self-authored.

LLM-compiled material has different epistemic status from hand-authored material. Mixing them blurs which claims Hugo stands behind vs. which the model produced. The Knowledge Base exists as a separate substrate precisely so this distinction stays sharp.

Compiled research can and should **link** into curated material — that's how the layers connect — but should never **modify** it.

## Sources are append-only

Within `Knowledge Base/Sources/`, files are never edited after creation, even by the skill itself. If a source needs correction:

- For factual error: capture a new source, supersede the compiled notes that drew on the old one
- For typo or formatting: leave it. Sources reflect what was captured at capture-time.

This is because Sources are evidence. Editing evidence retroactively breaks the audit trail.

The append-only rule governs **content, not location**. Relocating a source *within* `Sources/` — e.g. into a themed sub-folder (`Sources/Other/Health/`) — is allowed via `move_file`: the bytes are unchanged, only the path moves, and inbound wikilinks are rewritten so the audit trail stays intact. Moving a file *out* of `Sources/` (or *into* it from elsewhere) remains forbidden — those are `add`/supersession concerns.

## Index files are skill-managed

`index.md` files (top-level and per-subfolder) are written by the skill on every confirmed write. They are **catalogs**, not curated docs — Hugo doesn't hand-edit them. If audit detects index drift (file present, not catalogued, or vice versa), it proposes a regeneration.

The split between `index.md` (catalog) and `log.md` (chronological activity feed) is deliberate. Earlier KB versions mixed both into top-level `index.md`; as scopes accumulated, the activity feed dominated. `log.md` is the chronological surface now. `index.md` stays catalog-first.

Subfolder `index.md` files are catalog-only: a header, a brief one-line statement of what the folder holds (pointing to `_Schema/references/` for the page-type definition), then the entry list. Orientation prose ("why this layer exists," "how this differs from X," multi-paragraph rationale) belongs in the schema, not in indexes.

The skill never writes to `_Schema/SKILL.md` or anything in `_Schema/references/`. The schema evolves through human edit (or through this conversation). Self-modification is out of scope.
