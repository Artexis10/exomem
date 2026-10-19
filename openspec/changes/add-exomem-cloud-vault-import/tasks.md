## 1. Owner restore by runbook (D9)

- [x] 1.1 Inventory the cell volume's runtime state. List every path under `/data/host` that the runtime writes, and separate derived state and caches from custody, which must survive a restore. Verify: a short table in the runbook, measured on the cell image by running `cell-init` and the server on a scratch volume, writing and recalling a note, then listing the volume.
- [x] 1.2 Check the owner vault's governance schema on the source machine with `exomem governance-schema status`. Verify: the result is recorded in the runbook's evidence. If the vault is at a schema a cell refuses, run `downmigrate` on a copy and import the copy.
- [x] 1.3 Write the unpacker as a small, standard-library-only module used by both the runbook and later the import Job. It streams tar from stdin, refuses links, special entries, absolute paths, `..`, invalid names, duplicates, cap and free-space overruns before writing, requires the end-of-archive marker and optional declared counts, and reads its input to EOF. Verify: red-first unit tests for each refusal, for a stream cut between members, and for a clean archive; a refusal leaves no files; each guard is mutation-checked; the module runs standalone on the cell image's Python.
- [x] 1.4 Add `age` to the node's base packages. Verify: the Ansible defaults list it, and `age --version` runs on the node. Re-verified on 2026-09-30: `infra/ansible/roles/base/defaults/main.yml` includes `age`; the alpha node reports `/usr/bin/age`, version 1.1.1.
- [x] 1.5 Write `docs/runbooks/cloud-operator-import.md`: source checks, `/dev/shm` key, streamed transfer over SSH from the source machine, durable stop, unpack with the commit gate, swap with the prior vault kept, start, verification, cleanup, rollback. Every block must be shellcheck-clean and use break-glass only where needed. Verify: the privacy gate and shellcheck pass, and the swap, start, governed write and rollback are rehearsed on the cell image against a volume with existing runtime state.
- [ ] 1.6 Grow the owner cell's `storage_gib` to fit the new vault, the prior vault and derived state, and confirm the PVC expands online. Verify: the PVC and the filesystem inside the cell report the new size.
- [ ] 1.7 Run the runbook for the owner's vault. Verify: the cell restarts on the restored vault with custody intact; the owner's connector finds a known note and commits and reads back a governed write; notes only the prior vault held were shown to the owner; the key and ciphertext are deleted right after; the next backup succeeds within the Job deadline; only then is the prior directory deleted.

## 2. Cell-side import commands and adoption (D2, D6, D7, D8)

- [x] 2.1 Add a cell command that mints the import identity under `/data/.import-keys/<id>/` (mode `0600`) and writes only the public recipient to the termination message. Verify: unit tests for file mode, for the recipient format, for the path being outside the backup paths, and for no identity bytes in stdout, stderr or the message.
- [x] 2.2 Add a cell command that checks the ciphertext digest, decrypts with that identity, pipes it into the 1.3 unpacker, applies the commit gate, and moves the result into `_Imports/<id>/`, or swaps the vault in restore mode. Make `cell-init` refuse to create a vault while a `/data/.restore-prior-*` directory exists. Verify: tests for a clean import, a wrong key (`ARCHIVE_UNDECRYPTABLE`), a wrong digest (`ARCHIVE_DIGEST_MISMATCH`), a decryptor failing after a valid tar (nothing committed), a refused member leaving the vault byte-identical, restore keeping the custody directory, and `cell-init` refusing in the swap window.
- [x] 2.3 Remove `adopt_vault` from `CLOUD_SURFACE_EXCLUSIONS`, reword `transfer_artifact`'s lifting condition so the batch import path does not meet it, and amend the tool-surface scenario in the active change `adopt-exomem-cloud-plain-cells`, coordinating with whichever change archives first. Verify: the tool-surface tests show `adopt_vault` present on cells, and OpenSpec strict validation passes.
- [x] 2.4 Add a cell command that discards an import: its identity, its staging directory, or a staged `_Imports/` folder, permanently. Verify: tests that each is gone, not in trash, and that nothing outside the named import is touched.
- [x] 2.5 Confirm the import commands' logs are content-free. Verify: the content-free logging tests cover every command, with a canary member name that must not appear.
- [x] 2.6 Add an age implementation to the cloud image. Verify: an image test decrypts a fixture archive.

## 3. cellctl import support and contract C5 (D3, D4, D5, D8)

- [x] 3.1 Add `exomem_cloud_imports` to the fixture schema with the C5 columns, split state columns and grants, list it in the grants script beside C1, and add `import` to C1's `hold_kind` CHECK. Verify: fixture tests show cellctl cannot write Substrate's columns and Substrate cannot write cellctl's, and that default privileges grant Substrate nothing more.
- [x] 3.2 Render the prepare, import and discard Jobs: cell image, cell namespace, volume mount, no ServiceAccount token, and a presigned GET for the import Job only. Verify: red-first manifest tests for egress, security context, import-id validation and termination-message handling.
- [x] 3.3 Implement the import hold in the decision function, with a deadline, and the import flow in the reconcile loop:
  - `requested` → hold → prepare Job → recipient checked and recorded → multipart upload created and parts presigned → `key_ready`;
  - `uploaded` → completion with the reported ETags → hold → import Job → `done` or `failed`;
  - `cancelled`, `discard_requested` and expiry → hold → discard Job.

  Verify: reconcile tests against a disposable Postgres with the fake object store, including a restart mid-import that resumes the hold, a hold past its deadline, and a routine convergence pass that must not clear an import hold.
- [x] 3.4 Record cleanup_running before local cleanup and cleanup_stopping for keep-adoption cleanup or cleanup_discard_stopping for full discard before removing completion Jobs. Rearm full discard under the C1 and C5 locks when newer intent follows ordinary cleanup. Delete ciphertext versions, abort multipart uploads, and clear upload URLs. Wait for all phase Jobs and Pods, then publish current intent and release C1 atomically. Add a B2 lifecycle rule on the `cells/` prefix that sets only `daysFromStartingToCancelingUnfinishedLargeFiles`. B2 rules match a literal prefix, so the rule cannot name `imports/` alone, and it also clears restic's abandoned uploads. Verify: tests that no object, upload or identity remains after `done`, `failed`, `cancelled` and `expired`.
- [x] 3.5 Extend the live-K3s suite with an import: prepare, upload with presigned URLs, complete with the reported ETags, then import into a running cell. Also cover a refused archive, a wrong-key archive and a substituted part. Verify: the scenario passes in the live job, and the vault is unchanged after each refusal.
- [x] 3.6 Add a CORS rule on the Cloud bucket for the web app's origin: `PUT`, with `ETag` exposed. Verify: against B2 itself, with a disposable prefix, a preflight and a part upload from the origin succeed and one from another origin is refused.

## 4. Substrate flow (its own change) and rollout

- [ ] 4.1 Open the Substrate change for C5 and the C1 `hold_kind` value: migrations and grants, a "Bring your notes" flow that tars and age-encrypts a chosen folder in the browser, uploads it in parts and reports the ETags and digest, import status on the Cloud home page, and a discard action. Verify: the Substrate change is merged and deployed, and its tests cover encryption round-trips, resumable parts and the reported ETags.
- [x] 4.2 Add `exomem cloud import <folder>` for technical tenants, on the same C5 flow. Verify: an end-to-end run against a rehearsal cell imports a folder into `_Imports/`.
- [ ] 4.3 Import a friend-sized vault end to end in production with the owner's account, then adopt it through Adoption Studio and discard the staged folder. Verify: files are staged, adoption proposals appear, a filed note carries provenance to its original path, and nothing is left behind.

## Implementation evidence (2026-10-11, merged in PR #1692)

The source implements concrete Cloud owner binding, its persisted-job recheck,
the protected encrypted envelope, private receipts, age commands, C5 controller
Jobs and multipart transport, and a resumable OAuth CLI. Scoped tests exercise
real age, protected member recovery, ciphertext substitution, exact multipart
reports, cancellation precedence and disposable PostgreSQL split grants.

The built Cloud image passed a real age roundtrip with networking disabled.
Disposable PostgreSQL tests cover cleanup failure, controller restart, service
recovery, bounded retry and eventual cleanup. The real-K3s rehearsal passed an
accepted encrypted import into a running cell. A retained cleanup Pod kept
admission occupied and the runtime paused until every phase Job and Pod was
gone. Source hashes matched, and the identity, ciphertext and staging files
were absent after cleanup.

The CLI passed a trusted-TLS rehearsal with a synthetic OAuth server and PKCE.
Independent K3s rehearsals also refused an unsafe member after a valid member,
a wrong recipient, and a substituted ciphertext part. Each refusal preserved
the complete canonical vault manifest and removed temporary import resources.
The readiness correction passed a fresh accepted import. C1 reported running
and ready after key preparation and after the presigned ciphertext upload.
The controller kept admission occupied until all phase Jobs and Pods disappeared.

The maintained live suite now runs the accepted import and all three refusals
through one workflow. All four cases passed against the reviewed controller
and built Cloud image. Each refusal kept all 77 canonical vault entries
unchanged through cleanup and restart. PR #1692 merged this suite and the
reviewed import implementation. Task 3.5 is complete.
Production OAuth registration and production rollout remain open.
The one-node local storage rehearsal does not prove multi-node
storage behavior, large multipart uploads or production bucket restrictions.
The owner restore was previously performed; unchecked historical tasks do not
authorize repeating it.

## Native CLI acceptance (2026-10-12)

The optional native CLI journey passed against committed source `36632b461`.
The ordinary CLI used real OAuth with S256 PKCE and its loopback callback.
The real Substrate app recorded one completed C5 import through cellctl and K3s.
Both invented files matched their source hashes in `_Imports/`.
The private receipt recorded the declared two files and 90 bytes.
The identity, staging, checkpoint material, phase Jobs, Pods, ciphertext versions and
multipart uploads were absent after completion. The cell resumed running and ready.

The disposable CA layer preserved the source image's product and model bytes.
This bounded run selected only provisioning steps 1 and 2 plus the import journey.
It does not close the twelve-step launch rehearsal or its node gate.
Task 4.2 is complete; production OAuth, large imports, adoption and discard remain open.

## Bucket acceptance (2026-10-12)

PR #1703 added the bucket policy. The reviewed production plan changed only
CORS and lifecycle rules. Provider inspection confirmed private storage,
encryption and disabled completed-file expiry after the apply.
The lifecycle rule cancels unfinished uploads under `cells/` after two days.
Timed cancellation was not observed.

A real Chromium check used an invented disposable prefix against B2.
The configured origin passed preflight and uploaded one 32-byte multipart part.
JavaScript read the exposed ETag; it matched provider metadata and source bytes.
Another origin failed preflight and browser fetch; its different bytes did not
change the stored part. Cleanup removed the multipart upload and temporary key.
These results complete task 3.6.

PR #1692 supplies the cleanup implementation and its cancellation, expiry,
restart, late-intent and atomic-publication checks.
The live import rehearsal verified identity, staging and ciphertext cleanup.
The applied cancellation-only bucket policy completes task 3.4.
Production OAuth, the product browser flow, adoption, discard and large imports
remain open. This check did not run the production C5 flow.


## Command and controller proof (2026-10-12)

The stopped-cell command checks cover private identity permissions and placement,
content-free output, and permanent discard of only the named import.
A real age decryptor emitted a complete tar before its final chunk failed.
The command refused that archive and preserved the canonical vault bytes.
The ciphertext digest refusal preserved those bytes too.

The Job checks cover all import phases, their cell image and volume,
ServiceAccount token exclusion, import identifiers, and the import-only GET URL.
An empty preparation report keeps the import hold and publishes no recipient.
Disposable PostgreSQL checks verified the split grants and controller recovery.
The controller-only environment skipped two existing cross-package cases.
The earlier reviewed live-K3s import and refusal checks remain the runtime proof.

These checks complete tasks 2.1 through 2.6 and 3.1 through 3.3.
They extend the command, Cloud surface, image and controller evidence from PR #1692.
They change no product code, deadline, capability or production setting.
Owner restore, the product browser flow and production adoption remain open.
