## Why

A person with an existing collection asks their assistant to bring it into Exomem. The collection is notes, documents, images, audio and video, often an Obsidian vault. Adoption Studio is the path for this, on a personal install and on Cloud (`_Imports/<import_id>`). Today it loses most of what makes the collection useful:

- It imports text-like files only. It refuses every PDF, image, audio file and video as `UNSUPPORTED_IMPORT_TYPE`, so every embedded attachment is lost.
- It pastes each note inside a code fence. The note's links, embeds, tags and aliases then produce no graph edges and no tags. The fenced text also loses trailing whitespace and any bytes that are not valid UTF-8.
- It files every copy flat and unclassified in `Sources/Imported/`. A collection of 1,000 notes becomes 1,000 items of classification debt.
- A second run copies every file again, because only the current run is checked for earlier copies. On Cloud, a re-upload lands under a new `_Imports/<import_id>/` path, which no path-based check would match.
- `apply` commits the whole selection in one batch under one request budget. Collections with media do not fit in that budget.
- `finish` runs one keyword query and reports no coverage.

The data a person starts with decides how good every later answer is. This change makes the import lossless and the result visible. Agent enrichment (entities and typed relations) stays proposal-based and is out of scope.

## What Changes

- **Every file comes along.** Adoption Studio imports each selected file. A valid UTF-8 text-like file becomes a text Source whose fenced capture is the exact original text. Any other regular file becomes a Source that keeps the original bytes as its artifact, through one artifact seam shared with capture. Media extraction then runs through the existing media job path. A file that cannot be imported gets a per-path code; nothing is dropped silently.
- **The collection's structure survives.** For each imported note, the plan resolves its links and embeds against the collection. The imported Source gets a generated `## Links` section, outside the fenced capture, with links to the imported targets. Unresolved targets are listed as text. Front-matter tags and aliases become page tags and aliases. The fenced capture stays the exact original text.
- **Homes per folder.** A `select` folder rule can name a `source_kind` and a `domain` from the vocabulary registry. Files under that rule go to `Sources/<Kind>/<Domain>/`. Files without a rule stay in `Sources/Imported/`, unclassified, as before.
- **Re-runs do not duplicate.** Each import records a collection label and its path inside the collection. `plan` finds originals that an earlier run already imported with the same bytes, even from a new staging location. It reports them as `already-applied` with the earlier Source's ref and does not copy them again. A changed original imports as a new Source that names the earlier import.
- **`plan` and `apply` work in bounded calls.** Each call stops before the request budget ends and returns progress with a continuation cursor. The next call passes the cursor, so the replay cache never returns stale progress. Outcomes are written with the commits they describe, and an interrupted call is recovered by the identity check.
- **`finish` reports coverage.** The result counts outcomes by code, skipped hidden folders and junk, media extraction states, and resolved and unresolved links. Its recall checks match the exact imported page and cover a sample spread over the collection.
- **Guidance and staging path.** The scaffolded `exomem-ingest` workflow skill teaches the collection route. The `adoption_studio` tool description names every media kind. The hosted staging requirement names the Cloud staging folder `_Imports/<import_id>/`.

No change is default-off: the media extraction that imported files trigger is the existing automatic processing, with its existing per-deployment engine switches and soft-fail behaviour. No new model runs; the change adds no server-side reasoning.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `adoption-studio`: the scan and selection, the plan, the apply, the finish, the hosted staging intake and the originals rule change as listed above.

`adopt_vault`'s stateless modes (`copy-as-sources`, `compile-selected`) keep their text-only contract. They share the planner, so they also get the `## Links` section and the cross-run check.

## Impact

- **Exomem (`src/exomem`):** `adopt.py` (planner, rendering, identity check), `adoption_run.py` (inventory, folder-rule classification, bounded calls, coverage), `add.py` and `client_artifacts.py` (the shared artifact seam), the wikilink helper (alias, embed and fragment metadata), `commands.py` (tool description), the scaffolded `exomem-ingest` skill.
- **Tests:** adoption and adoption-run tests for each changed behaviour.
- **Benchmark:** `benchmarks/collection_ingest/` measures the change on an invented collection of about 1,000 notes with mixed media, before and after.
- **Storage:** imported attachments are copies, so a collection's attachments use disk twice until the original is removed. On Cloud, the staged `_Imports/<import_id>/` folder is discarded after the import.
- **Cloud:** the change ships in an ordinary release. Cloud cells get it through the normal cell rollout.
