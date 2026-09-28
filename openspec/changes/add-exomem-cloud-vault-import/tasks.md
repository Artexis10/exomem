## 1. Owner restore by runbook (D9)

- [ ] 1.1 Inventory the cell volume's derived state. List every path under `/data/host` and `/data/vault` that an Exomem runtime rebuilds from the vault, such as indexes, caches, graph checkpoints and the per-vault state key binding. Separate them from custody, identity and writer-lease state, which must survive a restore. Verify: a short table in the runbook, checked against a cell image by running `cell-init` and the server on a scratch volume, deleting the derived paths, and restarting cleanly with recall working.
- [ ] 1.2 Check the owner vault's governance schema on the source machine against what a standalone cell serves. Verify: the result is recorded in the runbook's evidence. If the vault is at a schema a cell refuses, stop and write a detach task before continuing.
- [ ] 1.3 Write the unpacker as a small, tested script used by both the runbook and later the import Job. It streams tar from stdin, refuses links, special entries, absolute paths, `..`, invalid names and cap overruns before writing, and writes into a staging directory. Verify: red-first unit tests for each refusal and for a clean archive, plus a refusal leaving no files.
- [ ] 1.4 Write `docs/runbooks/cloud-operator-import.md`: compatibility check, `/dev/shm` key, streamed transfer over the `desktop-wsl` peer connection, scratch rehearsal, swap with the prior vault kept, verification, backup, cleanup. Every block must be shellcheck-clean and use break-glass only where exec is needed. Verify: the privacy gate and shellcheck pass, and the blocks run against the live-K3s rehearsal cluster with a synthetic vault.
- [ ] 1.5 Grow the owner cell's `storage_gib` to fit the measured vault (attachments included) plus headroom, and confirm the PVC expands online. Verify: the PVC and the filesystem inside the cell report the new size.
- [ ] 1.6 Run the runbook for the owner's vault. Verify: the scratch rehearsal answers recall for a known note, the cell restarts on the restored vault with custody intact, the owner's connector finds a known note, and the next backup succeeds. Only then are the prior vault, ciphertext and key removed.

## 2. Cell-side import commands and adoption (D2, D6, D7)

- [ ] 2.1 Add a cell command that mints the import identity under `/data/host/imports/<id>/` (mode `0600`) and writes only the public recipient to the termination message. Verify: unit tests for file mode, for the recipient format, and for no identity bytes in stdout, stderr or the message.
- [ ] 2.2 Add a cell command that decrypts an archive stream with that identity, pipes it into the 1.3 unpacker, and moves the result into `_Imports/<date>-<id>/`, or swaps the vault in restore mode. Verify: tests for a clean import, for a wrong key (`ARCHIVE_UNDECRYPTABLE`), for a refused member leaving the vault byte-identical, and for restore keeping `/data/host`.
- [ ] 2.3 Allow `adopt_vault` on cloud cells for paths under `_Imports/` only, and replace its `CLOUD_SURFACE_EXCLUSIONS` entry with that restriction. Amend the tool-surface scenario in the active change `adopt-exomem-cloud-plain-cells`. Verify: the tool-surface tests show `adopt_vault` present and a path outside `_Imports/` refused with a typed error, and OpenSpec strict validation passes.
- [ ] 2.4 Confirm the import commands' logs are content-free. Verify: the content-free logging tests cover both commands, with a canary member name that must not appear.

## 3. cellctl import support and contract C5 (D3, D4, D5, D8)

- [ ] 3.1 Add `exomem_cloud_imports` to the fixture schema with the C5 columns, states and column grants. Verify: fixture tests show cellctl cannot write Substrate's columns and Substrate cannot write cellctl's.
- [ ] 3.2 Render the prepare, import and cleanup Jobs: cell image, cell namespace, volume mount, object-storage egress for the import Job only, no ServiceAccount token. Verify: red-first manifest tests for egress, security context and termination-message handling.
- [ ] 3.3 Implement the import flow in the reconcile loop:
  - `requested` → prepare Job → recipient recorded → multipart upload created and parts presigned → `key_ready`;
  - `uploaded` → import hold → import Job → `done` or `failed`.

  Also implement expiry and cancel. Verify: reconcile tests against a disposable Postgres with the fake object store, including a restart mid-import that resumes the hold.
- [ ] 3.4 Implement cleanup: delete the ciphertext versions, abort the multipart upload, run the cleanup Job, all on every terminal state. Verify: tests that no object, upload or identity remains after `done`, `failed`, `cancelled` and `expired`.
- [ ] 3.5 Extend the live-K3s suite with an import: prepare, upload with presigned URLs, then import into a running cell. Also cover a refused archive and a wrong-key archive. Verify: the scenario passes in the live job, and the vault is unchanged after each refusal.
- [ ] 3.6 Add a CORS rule on the Cloud bucket for the web app's origin: `PUT`, with `ETag` exposed. Verify: a preflight from the origin succeeds, and one from another origin is refused.

## 4. Substrate flow (its own change) and rollout

- [ ] 4.1 Open the Substrate change for C5: migration and grants, a "Bring your notes" flow that tars and age-encrypts a chosen folder in the browser and uploads it in parts, and import status on the Cloud home page. Verify: the Substrate change is merged and deployed, and its tests cover encryption round-trips and resumable parts.
- [ ] 4.2 Add `exomem cloud import <folder>` for technical tenants, on the same C5 flow. Verify: an end-to-end run against a rehearsal cell imports a folder into `_Imports/`.
- [ ] 4.3 Import a friend-sized vault end to end in production with the owner's account, then adopt it through Adoption Studio. Verify: files are staged, adoption proposals appear, a filed note carries provenance to its original path, and nothing is left behind.
