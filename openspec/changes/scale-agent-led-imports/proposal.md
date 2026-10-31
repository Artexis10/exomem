# Scale agent-led imports

## Why

Bringing a large amount of existing data into Exomem does not work well today, on a first import or on any later one. Three limits cause this:

- **Two full copies on disk.** An import first stages a full copy under `_Imports/<import_id>/`. Adoption then copies each file again into the Knowledge Base, and the staged copy stays until someone discards it. A cell whose volume holds the data once cannot file it, and a local import doubles the disk use in the same way.
- **5,000 files per run.** Adoption scans at most 5,000 candidates per run and reports the rest as truncated. The only way past that is splitting by folder, which fails for one flat folder that holds more than 5,000 files.
- **100 MiB per file on Cloud.** A Cloud cell refuses to adopt a staged file over 100 MiB, although the file is already on the cell. That limit belongs to client pulls through the gateway, not to staged files. Long recordings and videos are routinely larger, and a discard then deletes them.

The intended experience is that a person asks their agent to bring their data in, at any size and at any time, and the agent leads the whole flow. Remote and local clients get the same capability.

## What changes

- **Consume filed artifacts.** After adoption files an artifact from an import stage and verifies the Knowledge Base copy, it removes the staged copy. This applies only when the stage's manifest lists the file with the same path, size and hash. Notes and other text stay staged until discard: they are a small share of bytes. A folder that a person created is never consumed. A run can opt out.
- **Stage manifests.** The Cloud import Job lists each staged member in its receipt. The local `exomem import` command writes a stage record with the same list when an import finishes.
- **Recovery and parallel runs.** A missing staged copy resolves through the committed Source, so a crash or a parallel run never turns into a failure.
- **Paging.** `adoption_studio start` pages through any number of candidates with an opaque cursor that keeps one collection identity. Pages plan in order and never guess a link target.
- **Large files.** A new adoption ceiling replaces the client-pull cap for staged files on every runtime. Each artifact is copied in one streamed pass that hashes as it copies, so files over 100 MiB adopt on Cloud as they do locally.
- **Agent guidance.** The import tools and the Adoption Studio next actions guide an agent through the whole flow: import, wait, page, select, plan, apply, finish, review the residual and discard.

## Capabilities

### Modified Capabilities

- `adoption-studio`:
  - consume recorded staged artifacts after verified filing;
  - page the inventory under one collection identity;
  - bound space, memory and time per chunk;
  - record finished local stages;
  - report the residual at finish.
- `cloud-vault-import`: the import receipt lists each staged member.

## Impact

- **Code:** `adoption_run.py`, `adopt.py`, `adoption_plan.py`, `local_import.py`, `cloud_import_job.py` (it only reads its receipt), `client_artifacts.py`, `commands.py` (tool text), the skill scaffold reference and the Cloud rehearsal import scenario.
- **Out of scope:**
  - Substrate admission still multiplies declared bytes by two. Making that factor per kind is a follow-up in `publish-cloud-import-limits` and its Substrate pair.
  - Imports larger than the cell volume remain `add-cloud-media-store` task 5.4.
