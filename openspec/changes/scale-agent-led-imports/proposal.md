# Scale agent-led imports

## Why

Bringing a large amount of existing data into Exomem does not work well today, on a first import or on any later one. Three limits cause this:

- **Two full copies on disk.** An import first stages a full copy under `_Imports/<import_id>/`. Adoption then copies each file again into the Knowledge Base, and the staged copy stays until someone discards it. A cell whose volume holds the data once cannot file it, and a local import doubles the disk use in the same way.
- **5,000 files per run.** Adoption scans at most 5,000 candidates per run and reports the rest as truncated. The only way past that is splitting by folder, which fails for one flat folder that holds more than 5,000 files.
- **100 MiB per file on Cloud.** A Cloud cell refuses to adopt a staged file over 100 MiB, although the file is already on the cell. That limit belongs to client pulls through the gateway, not to staged files. Long recordings and videos are routinely larger, and a discard then deletes them.

The intended experience is that a person asks their agent to bring their data in, at any size and at any time, and the agent leads the whole flow. Remote and local clients get the same capability.

## What changes

- After adoption commits an item's Source and verifies it, adoption removes that item's staged copy. This applies only to a stage that an import created and recorded. A folder that a person created, inside `_Imports/` or anywhere else, is never consumed, so the bytes on disk stay close to one copy throughout filing. A run can opt out of consuming.
- The local `exomem import` command records each stage it creates in the vault, so local stages qualify the same way as Cloud stages.
- `adoption_studio start` takes an `after` cursor and returns `next_after`, so a run can page through any number of candidates in a stable order.
- Adoption reads staged files by streaming and applies the same per-file limit on every runtime. Staged files over 100 MiB adopt on Cloud as they do locally.
- The import tools and the Adoption Studio next actions guide an agent through the whole flow: import, wait, page, select, plan, apply, finish, review the residual and discard.

## Capabilities

### Modified Capabilities

- `adoption-studio`: These requirements change: originals stay untouched except for recorded staged copies, which are consumed after filing; the inventory pages; staged files are not limited by the client-pull cap; and finish reports the residual.

## Impact

- **Code:** `adoption_run.py`, `adopt.py`, `adoption_plan.py`, `local_import.py`, `cloud_import_job.py` (it only reads its receipt), `client_artifacts.py`, `commands.py` (tool text), the skill scaffold reference and the Cloud rehearsal import scenario.
- **Out of scope:**
  - Substrate admission still multiplies declared bytes by two. Making that factor per kind is a follow-up in `publish-cloud-import-limits` and its Substrate pair.
  - Imports larger than the cell volume remain `add-cloud-media-store` task 5.4.
