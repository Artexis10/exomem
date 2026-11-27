# Import app exports

## Why

A person can drop any file on Home, and the cell files it as a Source. An export from another app arrives as one file that holds hundreds or thousands of items. A ChatGPT or Claude data export keeps every conversation in one `conversations.json`. That file is usually larger than the 4 MiB inline-text limit, so the cell files it as one artifact Source with no searchable text and the date of the upload. The person's conversations do not become searchable, and nothing records when each one happened.

No filing path turns one file into several Sources today. No registry recognises a file by its vendor format during adoption, and a filed Source has no field for the time its content was written.

## What changes

- **A `source-formats` registry recognises app exports.** Each entry names one vendor export format: the file name pattern, the JSON keys that a vendor's items always carry, the parser that reads it and the Source kind of each item. The set is closed per vendor format and lives in a shipped pack with a vault overlay, like `sync-providers`.
- **Filing expands a recognised export before it selects files.** At the start of `file`, the cell streams each recognised export and writes one Markdown transcript per item into the stage, named by the vendor's item id. The step is resumable within the call budget. Each derived file then flows through the normal plan, dedup, apply and left-out accounting.
- **Each item keeps its own time.** A new provenance page field records when the item was created, from the export. `captured` stays the filing time.
- **The export itself is kept.** The original export file is still filed once, so its exact bytes stay in the vault. Each derived Source names the export it came from.
- **Partial and failed exports stay visible.** An item that cannot be read is counted as failed, with a reason. A truncated or malformed export keeps the items read before the fault, and the filing view says the export was read only in part.
- **First formats:** ChatGPT and Claude conversation exports.

Four small corrections from the previous import change ride in the same batch:
- The read-surface inventory records the filing classifier's reads instead of exempting its arguments.
- `write_intake._replay` goes through `invoke_in_background`.
- A resumed CLI import with a checkpoint from another source reports `CHECKPOINT_MISMATCH`.
- The Cloud home-import rehearsal restarts the cell while filing is still working, so the exactly-once claim runs live.

## Capabilities

### New Capabilities

- `app-export-import`: recognise a vendor export by registry and file each of its items as its own Source, with the item's own time.

### Modified Capabilities

- `page-field-registry`: a provenance field may take the time an item states for itself.

## Impact

- **Code:**
  - `src/exomem/vocabulary/packs/core/source-formats.yaml` and `page-fields.yaml`.
  - A new `source_formats.py` holds the registry and the parsers.
  - `adoption_run.py`: the expand step in `_file_step`, item kind and time on planned items, and the stage total.
  - `adopt.py`: the provenance lines.
  - `import_filing.py`: partial-read reporting.
  - The three corrections: `tests/test_vault_store_read_surface.py`, `write_intake.py`, `cloud_import_client.py` and `infra/cloud-rehearsal/src/cloud_rehearsal/home_import.py`.
- **Unchanged:** the import upload contract, Home, the `file` action's arguments and the C5 import contract.
- **Follow-ups:**
  - Home shows plain "how to get your export" steps per app, as a Substrate change.
  - More formats follow on this registry: Notion, Google Takeout, Evernote, WhatsApp and Mem.
  - A later re-export of the same conversation with new messages gets a new Source. Linking it to the earlier one is a later change.
