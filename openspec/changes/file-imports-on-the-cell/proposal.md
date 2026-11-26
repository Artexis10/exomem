# File imports on the cell

## Why

A person without a terminal or an AI assistant cannot finish an import today. Home uploads a folder and the cell stages it under `_Imports/<id>`. After that, nothing becomes searchable until a connected assistant runs Adoption Studio's `file` action, page by page. Home tells the person to paste a prompt into ChatGPT or Claude. A person who uses neither is stuck with staged files that nothing reads.

An import also forgets where its files came from. The browser writes each file's relative path, but not its modification time. Nothing records which device and browser sent the files, or the person's own label for them ("Photos from my phone", "Notion export"). Once filed, a Source keeps its stage path and collection path, and nothing else about its origin.

## What changes

- **The cell finishes a registered import by itself.** A resident filer in the serving runtime drives the existing `file` action until filing is done or stops. An import registers with the filer in two ways: its stage's series file asks the cell to file it, or any `file` call names the stage. The filer survives a restart: on start it finds every registered stage with work left and carries on from the saved phase. It never starts a second run, and it takes turns with foreground `file` calls.
- **A cheap filing record answers polls.** After each call, the filer writes one small per-import record: state, progress, stop, person, the time it was written and the measured rate. A status read by stage path returns that record without walking runs or outcomes, so Home and agents can poll it at any population.
- **Imported Sources record their origin.** The series file can carry an `origin` envelope: the person's label, and the coarse device and browser family of the upload. Filing writes the envelope and each file's modification time into the Source's frontmatter. The field names are registry data: page-field entries with a new `provenance` role.
- **The CLI asks for the same thing.** `exomem cloud import` writes the filing request and its origin into the series file, so a cell with the filer finishes the import even after the command exits.

## Capabilities

### Modified Capabilities

- `adoption-studio`: the cell files registered imports by itself; a filing record answers status; imported Sources record their stated origin.
- `page-field-registry`: a closed `provenance` role names the origin fields that filing writes.
- `cloud-vault-import`: the CLI's series file asks the cell to file and states the origin.

## Impact

- **Code:** a new `import_filing.py` worker started in `server_runtime.py` next to write intake; `adoption_run.py` (registration on `file`, the status read of the filing record); `import_stage.py` (series-file fields); `adopt.py` (`_provenance_lines`); `cloud_import_client.py` (series fields); `vocabulary/packs/core/page-fields.yaml`; the tool text in `commands.py`; the Cloud rehearsal import scenario.
- **Unchanged:** the C5 import contract, the `file` action's per-call behaviour, paging and throughput (owned by `scale-agent-led-imports`), and discard.
- **Follow-ups:**
  - The Home side lands as its own Substrate change: drop zone, files and zips, the series file, and the result view.
  - Undoing a filed import (erasing its Sources) lands as its own change, because discard removes only unfiled staged copies.
