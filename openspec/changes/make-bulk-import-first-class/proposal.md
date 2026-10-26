## Why

A person with an existing collection of notes and documents should only need to point their assistant at a folder, or upload it, and Exomem does the rest. Adoption Studio now imports a collection losslessly, but two gaps keep the result from being useful:

- Everything lands as a Source. A person's own notes are knowledge they already wrote, yet they arrive as captured material with no note identity. A collection of 1,000 notes becomes 1,000 Sources that nothing points back to as notes.
- Imported Sources join a review backlog that nobody acts on. The `unprocessed_source` audit family is not part of the maintained due-state projection, so bootstrap and the write advisory never show it. The other due families reach the agent only as a short list with no bounded way to act on many items at once.
- The backlog also reports false completion. A draft note that cites a Source writes the Source's `ingested_into` back-reference, and the audit then treats the Source as compiled. Nothing was compiled.
- A folder outside the vault has no import route on a personal install. `adoption_studio start` scans vault-relative paths only.

## What Changes

- **The agent decides what is a note.** A `select` folder rule can say `adopt_as: note` with a registered `note_type`. Files under that rule are the person's own notes. The agent makes the call from the scan's measurements and the folder layout; the server validates the note type against the registry and never infers meaning from words.
- **An own note becomes a governed draft note, and its original stays a Source.** For each own-note item, `plan` previews two targets: the lossless Source copy (exactly as today) and a draft note whose body is the original note body, citing that Source. `apply` creates both in the same bounded calls. The note's status is `draft` because the import makes no compilation claim. Links between own notes resolve to the planned notes. Each item records its note identity, so a retry or a re-run never creates a second note.
- **One plan confirmation covers the import.** Own-note creation is part of the exact adoption plan that `apply` already binds to `plan_id`. It only creates new pages and never edits, moves or deletes existing ones. Curation keeps its exact-plan confirmation for restructuring.
- **A Source is compiled only when a live note compiles it.** `ingested_into` stays a citation edge. The `unprocessed_source` family counts a Source as compiled only when at least one existing derivative has a live status class and its own `sources:` claim resolves to that Source. A draft derivative keeps the Source in the family and is reported as the next action.
- **The backlog reaches the agent in bounded, grouped batches.** `unprocessed_source` joins the maintained due-state projection, and a write to a note refreshes the Sources it cites. The served block adds the outstanding count beside the visible count, so dismissing and snoozing never look like progress. Bootstrap's compact core reports per-category counts and one route to the next batch. The existing `review_memory` modes return each batch with titles, bounded excerpts, existing derivatives and recorded grouping metadata (import collection, collection folder, source kind, domain, capture date). The agent groups the batch by topic and acts through the existing governed writers. No new tool or input parameter is added.
- **Collection reflection stays honest.** An `unreflected_observations` item still clears only through a declared collection reflection, never through a note.
- **A local folder outside the vault imports through staging.** `exomem import <folder>` copies the folder into `_Imports/<import_id>/` with retained no-follow custody and a manifest, after a preflight that reports file count and bytes against the effective limits. Adoption then runs on the staged folder, as on Cloud. `adoption_studio start` refuses a path outside the vault with a code that names this route.

- **The Cloud CLI works with no setup and reports what it does.** `exomem cloud import` and `exomem cloud export` default to the production Cloud endpoints, so a user sets no environment variables; flags and the existing variables still override them. `exomem cloud import` reports the preflight count and bytes, then progress for each phase (archive and encryption, upload, server import), and every failure with its phase, reason and import id.

No server-side model runs, and no vocabulary enters code. Grouping uses stored metadata and the agent's judgement.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `adoption-studio`: own-note selection rules, the plan and apply of draft notes, the outside-vault refusal and the local staging route.
- `attention-queue`: the source-coverage predicate, `unprocessed_source` in the due-state projection, honest counts, and the bounded grouped work item.
- `compiled-source-lineage`: `ingested_into` is a citation edge, not a completion claim.
- `cloud-vault-import`: an added requirement for the CLI's default endpoints, preflight and phase progress. The capability's other requirements stay in the active `add-exomem-cloud-vault-import` change, which Cloud owns.

## Impact

- **Exomem (`src/exomem`):** `adoption_run.py` and `adoption_plan.py` (own-note rules, plan, apply, identity), `note.py` (draft-note creation from an import item), `audit.py` (coverage predicate), `due_state.py` and `attention.py` (projection category, counts, work item), `bootstrap_core.py` and the write advisory (counts and route), the CLI (`exomem import`), tool descriptions and the scaffolded `exomem-ingest` skill.
- **Existing vaults:** a Source whose only derivatives are drafts, or whose back-reference names a page that no longer exists, now appears in `unprocessed_source`. The backlog count can rise; that is the honest count.
- **Cloud:** the change ships in an ordinary release and reaches cells through the normal rollout. The Home import page's preflight, progress and resume work is a separate Substrate change.
