# Publish Cloud import limits

## Why

The limits that decide whether a cell accepts an import live in the cell image and in cellctl. No client can read them, so a folder is encrypted and uploaded before the cell refuses it. A cell volume also cannot grow for an import. The import Job reports the files and bytes it staged, and cellctl discards that report.

## What changes

- cellctl publishes each cell's effective import limits in the C5 column `import_limits`. It rewrites the column only when a value changes.
- One configured size cap, held in `StorageConfig`, bounds growth for every storage class. A fleet with only Hetzner volumes gets a cap too.
- cellctl passes the member limit and the free-space reserve to the import Job, so the Job enforces the values that cellctl publishes.
- cellctl waits for the volume to grow before it mints the import key. The wait leaves the cell mounted, tolerates one pod restart and ends at a configurable deadline.
- cellctl records the staged file and byte counts from the Job's report on the import row.
- The Adoption Studio status rows carry each run's `source_root`, so Home can count the files filed from an import.
- The CLI reads the published limits before it creates an import, falls back to its built-in defaults against an older server, and maps the three new refusals.
- The contract pin moves to the Substrate migration that adds the new C5 columns.

## Decision

cellctl publishes each cell's effective limits. Admission, in the paired Substrate change, grows `storage_gib` within the published cap before `key_ready`. The `key_ready` observation waits for observed growth.

## Non-goals

- Plan-storage entitlement enforcement and the paid-storage baseline. The operator deferred both on 2026-10-26. Growth on demand can therefore take a cell of any plan up to the published cap.
- Adopted counts in C5. Adoption Studio files the staged notes after C5 reports `done`, so Home reads that count through the owner channel.
- Chart settings for the member limit and the reserve. The published values are the image defaults until a later change makes them configurable.

## Capabilities

### Modified Capabilities

- `cloud-vault-import`: Add the published limits, the one size cap, the wait for growth, the Job's limits, the staged counts and the CLI's use of the limits.
- `adoption-studio`: Add `source_root` to the compact status rows.

## Impact

The change touches `infra/cellctl`, the platform chart, `cloud_import_job.py`, `cloud_import_client.py`, `adoption_run.py` and the Substrate contract pin. The paired Substrate change `admit-cloud-imports-against-published-limits` adds migration 0079, the admission code and the Home flow. The Cloud control-plane owner reviews the cellctl changes, the chart value and the C5 text.

Archive order: the `cloud-vault-import` capability exists only in the active change `add-exomem-cloud-vault-import`, so this change adds its requirements with `ADDED`. Archive `add-exomem-cloud-vault-import` first. Archiving this change first would create a `cloud-vault-import` spec that holds only these requirements. The `adoption-studio` capability is canonical.
