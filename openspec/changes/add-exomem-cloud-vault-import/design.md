## Context

See proposal.md for why. The constraints this design works within, all from the Cloud cell design (`adopt-exomem-cloud-plain-cells`) and the operator-access boundary (`cloud-operator-access`):

- **Cell runtime.** It registers only MCP and health routes. It has no egress, not even DNS, and no control-plane access. Its volume holds `/data/vault` (the vault) and `/data/host`, the runtime's home. `/data/host` holds caches, the derived state under `.local/state/exomem/state/<vault-key>/`, and, when a vault is enrolled in standalone custody, `.local/state/exomem/standalone-host-control-v1/`.
- **Backup and restore Jobs.** They run on the cell image in the cell's namespace, with object-storage egress only. They authenticate as the cell's prefix-restricted B2 key. Backups cover `/data/vault` and `/data/host`, nightly and before upgrades.
- **Jobs and the volume.** The cell's ResourceQuota admits exactly the runtime's requests. The volume is ReadWriteOnce, and admission forbids pinning a Job to a node. So no Job can run beside the runtime, and every Job that mounts the volume runs under a hold that scales the runtime to zero first. Backups already work this way.
- **cellctl.** It is row-driven, with no API. It holds each cell's B2 key while it reconciles, and it records holds (`backup`, `upgrade`, `restore`) on the row.
- **Substrate's lifecycle reconcile.** It recomputes `desired_state` from the entitlement on every billing event and sweep. An operator write to `desired_state` does not stay put.
- **Operators.** The everyday identity is read-only and cannot exec into cell or scratch namespaces. Break-glass is minted per task and audited.
- **`cell-init`.**
  - It creates the vault only when the volume has none, then migrates state.
  - A vault at governance schema v4 either refuses writes or refuses recall in a standalone cell. The schema lives in the machine-local governance store, outside the vault. `exomem governance-schema status` reports it, and `downmigrate` takes a vault back.
  - The per-vault state key is a hash of the vault path, so a vault swapped in at `/data/vault` inherits the previous vault's derived state unless that state is removed.
- **`adopt_vault`.** It scans a vault subtree and copies or compiles chosen files into the governed Knowledge Base layer, keeping original paths and hashes. It is excluded from cells until an upload-then-adopt path exists.

## Goals / Non-Goals

**Goals:**
- Tenant imports where no operator-controlled component holds the import key or the plaintext, other than the cell's own volume. That is the same boundary as the vault itself.
- Key minting, decryption and unpacking all happen in the cell's own pods, so the parts that must move into a confidential cell later are already inside the cell.
- The owner's vault imported first, by an operator runbook that needs no new cellctl or web code.

**Non-Goals:**
- Converting other tools' export formats (Notion, Evernote, Roam). Markdown and plain files come in as they are, and adoption works on them. Converters are a later change.
- Continuous sync from a desktop vault. An import is a one-shot, point-in-time copy.
- Changing the operator trust model. Confidential cells are a separate change, and they need more than this design provides (see Risks).

## Decisions

### D1. Two paths, one pipeline

There are two paths:

- **Tenant import.** It lands in a staging folder, for adoption.
- **Owner restore.** It replaces the Cloud owner's own cell vault with the owner's existing Exomem vault.

Both share the same pipeline: encryption to a held key, ciphertext-only transfer, streaming decrypt-and-unpack with validation and a commit gate (D6), and a stopped cell. They differ in where members land and in who mints the key.

The node-minted key of D9 is allowed only for the owner's own vault. There, the operator and the tenant are the same person. Any other user's restore uses the cell-minted key and the cell's restore command (task 2.2), so the operator never holds a key that decrypts another person's archive. That mirrors the export rule in `cloud-operator-access`.

*Rejected: one mode that always replaces the vault.* A friend's notes are rarely an Exomem vault. Writing them over a cell that may already hold notes loses data, and it skips the adoption step that files them with intent.

### D2. The cell mints the tenant's import key under a short hold

cellctl takes an `import` hold (D5) and runs a **prepare Job** on the cell image in the cell's namespace:

1. It generates an age X25519 identity.
2. It writes the identity to `/data/.import-keys/<import-id>/identity`, mode `0600`, owned by the cell user. That directory is outside the backup paths, so the identity never reaches object storage.
3. It writes the public recipient to the container's termination message.

cellctl reads the recipient from the pod status, checks it is an age X25519 recipient, and records it on the import row. It then releases the hold and the cell starts again. The termination message already carries backup snapshot IDs the same way. It is the only thing that leaves the pod, and it holds no secret.

The hold costs the tenant a few seconds of downtime. Running the Job beside the runtime is not possible (see Context).

*Rejected: cellctl mints the key.* The key would then live in the controller's memory and, wrapped, in the database. That is a second place holding a tenant secret, and it would not move into a confidential cell.

*Rejected: an MCP tool on the cell.* The runtime cannot tell the control plane that an import exists, and the web flow cannot call MCP without holding a tenant token.

### D3. Direct multipart upload; cellctl completes it with the tenant's ETags

For each import, cellctl:

1. creates an S3 multipart upload for `cells/<cell-id>/imports/<import-id>.age`, using the cell's own key;
2. presigns one PUT URL per part, valid for 24 hours;
3. writes the part URLs to the import row.

The client encrypts and uploads the parts directly, and reads each part's ETag. When it marks the import uploaded, it reports the part ETags and the ciphertext's SHA-256 and size through its authenticated Substrate session. cellctl then completes the multipart upload itself with those ETags. The import Job checks the SHA-256 before decrypting anything (D5).

Uploads resume within one page session. age ciphertext is randomised, and the browser's SHA-256 is not incremental across reloads, so a reload starts the upload again. `exomem cloud import` is the path for vaults too large for that.

The web app and the gateway never carry the bytes. The Cloud bucket gains a CORS rule for the web app's origin that allows `PUT` and exposes `ETag`. A B2 lifecycle rule cancels unfinished large files after two days, as a backstop for uploads nobody completes.

*Why the completion moved to cellctl.* age gives confidentiality, not sender authenticity. The recipient is a public key and sits on the same row as the part URLs. Someone holding a leaked part URL and the recipient could upload a validly encrypted archive of their choosing, and decryption alone would not notice. The ETags and digest arrive through the tenant's authenticated session. A substituted part then fails the completion or the digest check.

*Rejected: upload through the gateway or the web app.* It puts plaintext-sized request bodies through operator-controlled code. It also runs into Vercel's request limits for multi-gigabyte archives.

### D4. Contract C5: the import request row

A new table, `exomem_cloud_imports`, joins Substrate and cellctl the same way `exomem_cloud_cells` does (C1). `import_id` is a UUID, and cellctl validates it before it appears in any path or Job. Column grants follow C1's split between what is wanted and what was observed:

- **Substrate writes:**
  - the request: `cell_id`, `kind`, `declared_bytes`, `declared_files`;
  - the tenant's report: `part_etags`, `ciphertext_sha256`, `ciphertext_bytes`;
  - `requested_state`: `requested`, `uploaded`, `cancelled` or `discard_requested`.
- **cellctl writes:**
  - `recipient`, `upload_id`, `part_urls`, `urls_expire_at`;
  - `observed_state`: `key_ready`, `importing`, `done`, `failed`, `expired` or `discarded`;
  - `error_code`.

Other rules:

- Nothing in the row is plaintext content.
- One import per cell may be open at a time.
- cellctl clears the part URLs when the import reaches a terminal state.
- The row outlives the import as a content-free record.
- The grants script lists the table beside C1's, so default privileges do not give Substrate full DML on it.

### D5. An `import` hold stops the cell for every import Job

The prepare, import and discard Jobs each run under an `import` hold, following the backup hold's pattern:

1. cellctl records the hold on the row, scales the runtime to zero, and waits until no pod uses the volume;
2. it runs the Job on the cell image;
3. it releases the hold and starts the cell again, on success or failure.

The import Job reads the ciphertext twice through a presigned GET that cellctl gives it. The first pass only computes its SHA-256, so no byte an attacker could have substituted reaches the decryptor or the tar parser before the digest matches. The second pass streams it through `age -d` into the validating unpacker (D6). Neither pass stores the ciphertext. The identity comes from the volume.

This needs:

- `import` added to C1's `hold_kind` CHECK, which is a C1 migration;
- an `import` branch in cellctl's decision function, so an import hold is continued, never cleared by routine convergence;
- a deadline, after which cellctl fails the import, releases the hold and starts the cell. A cell never stays stopped because an import hung.

Upgrades and backups already skip a held cell.

### D6. A validating streaming unpacker, and a gate before anything is committed

The unpacker reads the tar stream member by member. It checks every member before writing any of its bytes. It refuses the whole import on:

- links, devices or other special entries;
- absolute paths or `..` components;
- names a cell cannot hold: control characters, invalid UTF-8, components over 255 bytes, paths over 4096 bytes;
- a second member at a path already written, or a member beneath a file;
- going over the declared or maximum size or member count, or over the volume's free space less a reserve.

It accepts a plain or compressed tar stream (gzip, bzip2 or xz). Sizes stay bounded by the member headers, the caps and free space, and the hold's deadline bounds the time decompression can take.

It writes into `/data/.import-<import-id>`, a new directory on the same volume, never into the vault. On any refusal it removes that directory. On success it syncs the filesystem before reporting, because the tenant path deletes the ciphertext once the import is committed.

The staging directory is committed only when all of these hold:

- the decryptor exited 0. age authenticates its final chunk only at the end of the stream, so the unpacker reads its input to EOF;
- the tar end-of-archive block was read. Python's `tarfile` reads a stream that stops between members as a clean end, so the unpacker tracks the end block itself;
- the file and byte counts match what the sender declared, and the ciphertext's SHA-256 matched before decryption.

Where the committed result goes depends on the path:

- **Tenant import.** One `rename` moves the staging directory to `/data/vault/_Imports/<date>-<import-id>/`. Nothing existing is touched.
- **Restore.** The current vault and the vault's derived-state directory are moved into `/data/.restore-prior-<import-id>/`, and the staging directory is renamed to `/data/vault`. The runtime rebuilds derived state on start. The custody directory beside the derived state stays where it is.

The two restore renames are not atomic together. While a `/data/.restore-prior-*` directory exists, `cell-init` refuses to create a vault, so a cell started in that window fails visibly instead of initialising an empty vault. That guard ships with the cell commands (section 2). The owner restore runs on an image without it, and relies on cellctl staying paused until the swap is done (D9).

The prior directory is kept until the restored cell has passed recall, a governed write and a backup, then deleted. Rolling back removes the derived state the restored cell has written since it started, then moves the prior vault and derived state back.

### D7. Adoption: the `adopt_vault` exclusion is lifted

`adopt_vault` becomes available on cloud cells, without a path restriction. Its own lifting condition is met. Everything it can read belongs to the tenant, and `adoption_studio` already runs on cells without one. The tenant's assistant scans an import folder, and Adoption Studio proposes how to file it. Filing uses governed writes, with provenance to the original path and hash.

`transfer_artifact`, `process_media` and `read_media` stay excluded. `transfer_artifact`'s lifting condition names an artifact-upload path to Cloud, and this change adds one, but the tool could not use it. The import path is a batch Job under a hold that ends in a staging folder, not a live bridge a running cell's tool can hand a file to. Task 2.3 rewords that exclusion's condition to say so.

### D8. Cleanup, discard and failure

When an import ends, whether `done`, `failed`, `cancelled` or `expired`, cellctl:

- deletes the ciphertext object, every version, as in verified deletion;
- aborts any open multipart upload;
- deletes the identity and any staging directory. The import Job does this itself on completion. For a cancelled or expired import, cellctl runs a discard Job under the hold.

A tenant can also discard a staged import folder completely. Exomem's own deletes only move files to trash, and a mistaken import, such as a folder holding secrets, must not stay in the vault and in every nightly backup. A `discard_requested` row makes cellctl run the discard Job for that folder. Backups age the folder out under their normal retention.

A failed import leaves the vault exactly as it was. Error codes are typed and content-free:

- `ARCHIVE_UNDECRYPTABLE`;
- `ARCHIVE_DIGEST_MISMATCH`;
- `ARCHIVE_UNREADABLE`;
- `ARCHIVE_MEMBER_REFUSED`;
- `ARCHIVE_TOO_LARGE`;
- `STORAGE_ALLOWANCE_EXCEEDED`;
- `IMPORT_EXPIRED`;
- `IMPORT_HOLD_EXPIRED`.

Import logs carry counts, sizes, durations and codes only.

### D9. The owner restore runs before any of the above exists

The owner's vault comes in through `docs/runbooks/cloud-operator-import.md`. It uses only what exists today: break-glass, the cell image, `age` on the node, and an SSH connection from the operator's machine to the source machine. It applies only to the owner's own vault (D1). The steps:

1. **Check the source.** On the source machine:
   - read the governance schema with `exomem governance-schema status`, and stop if a standalone cell cannot serve it. A vault at v4 goes back with `downmigrate` on a copy;
   - record the source's Exomem version and the cell image's;
   - count files and bytes, and find links and special files. Any one of them refuses the import, so they are resolved on the source first;
   - decide what stays out, such as `.git`. Attachments come in.

   The import is a point-in-time copy. The source keeps running, and writes after the copy are not in Cloud.
2. **Size the cell.** Grow `storage_gib` to hold the new vault, the prior vault, and derived state while both exist.
3. **Mint the one-time key.** `age-keygen` on the node, into `/dev/shm`.
4. **Stream.** On the source machine, tar the vault and pipe it through `age -r <recipient>`. The ciphertext streams through the operator's machine to a file on the node, and its SHA-256 is recorded at both ends. The operator's machine only ever sees ciphertext.
5. **Stop the cell durably.** Pause cellctl by scaling its Deployment to zero, then scale the cell's StatefulSet to zero and wait until no cell pod exists. A `desired_state` write would be rewritten by Substrate's reconcile, and cellctl would then start the runtime mid-swap. Pausing cellctl also pauses backups and upgrades for every cell for the few minutes of the swap. Only a platform chart deploy would bring it back early, so none runs during the window. A cellctl back early would restore the cell's replica at once and start the runtime over a half-swapped vault.
6. **Unpack and swap.** A helper pod on the cell image mounts the cell's volume. The unpacker is piped into it, because the running image predates it. The node decrypts, and break-glass exec streams the plaintext into the unpacker. The commit gate and the swap follow D6. The operator checks that no cell pod exists before each volume change.
7. **Start.** Scale the StatefulSet back by hand, then resume cellctl. cellctl would also restore the replica when it resumes: the row still reads `running`, and cellctl applies a served cell that runs no replica again. `cell-init` migrates state, and the runtime rebuilds its indexes.
8. **Verify.** Through the owner's own connector, a known Knowledge Base note is found, and one governed write is committed and read back. A vault the cell cannot serve can refuse every write while recall still works. Before the swap, the operator lists notes in the prior vault that the restored vault lacks, and shows the owner before anything is deleted.
9. **Clean up.** The node key and the ciphertext are deleted as soon as step 8 passes: the prior vault is the rollback, not the ciphertext. The prior directory is deleted after the next scheduled backup succeeds. That backup's duration is checked against the backup Job's deadline, because the owner's cell is the rollout canary and a backup that cannot finish would stall every upgrade.

*Rehearsal.* The swap is rehearsed on the cell image against a volume with existing runtime state: the literal steps 6 and 7, recall of a known note, a check that the prior vault's notes no longer answer, a governed write, and the rollback. In production, the prior vault and its derived state are kept, so a restore that fails to start is reversed in minutes. The rehearsal cannot show the owner's real vault against the cell's memory limit and start-up deadlines. A failure there is a failure to start, which the rollback reverses.

*Rejected: a scratch-namespace rehearsal before the real swap.* It re-runs the same swap on a copy, which costs a rendered scratch cell, a port-forward and a probe. Its only extra coverage is a start failure, and the kept prior vault already reverses that.

## Risks / Trade-offs

- **The import key sits on the tenant volume, which the operator can read deliberately.**
  - This is the same boundary as the vault, and the spec's privacy requirement already discloses it.
  - Confidential cells close it for both at once, but not with this design alone. The recipient reaches the tenant through the termination message, cellctl, the row, Substrate, and browser code that Substrate serves. None of those is authenticated to the tenant. A confidential design needs a recipient attested by the cell, and a client the operator does not serve.
- **Browsers encrypting multi-gigabyte folders.**
  - Mitigation: streaming tar plus age encryption with bounded memory, and resumable multipart parts.
  - `exomem cloud import` is the path for very large vaults.
- **Removing too little or too much derived state in a restore.**
  - Mitigation: the derived-state directory is found through the same seam the runtime uses, and moved aside rather than deleted.
  - The rehearsal checks that the prior vault's notes no longer answer.
- **A vault at governance schema v4.**
  - Mitigation: the source check refuses it before anything moves, and `downmigrate` on a copy takes it back.
- **Part URLs on the import row.**
  - Each is write-only for one part of one object, and expires.
  - A substituted part fails completion or the digest check (D3).
  - The row readers are Substrate and cellctl, which already hold stronger credentials.
- **Downtime during import holds.**
  - Seconds for prepare and discard, minutes for an import.
  - The tenant starts it and sees its state, and the hold has a deadline.
- **Pausing cellctl during the owner restore.**
  - Every cell's reconcile waits for the swap.
  - Acceptable for a one-time restore measured in minutes. It must not become the tenant path.

## Migration Plan

1. **Owner restore by runbook (section 1 of tasks).** It changes no product code except the unpacker, and uses break-glass on the node.
2. **Cell-side commands and adoption (section 2).** These ship in a normal Exomem release, and cells pick them up through the rollout.
3. **cellctl import support and C5 (section 3).** This ships with the platform chart. The Substrate migrations for C5 and the C1 `hold_kind` value land first, because cellctl reads the C5 request columns and writes only its own.
4. **Substrate web flow (section 4).** This is its own Substrate change. Tenant imports switch on when it deploys.

Each step is additive. If step 3 or 4 misbehaves, turning off the Substrate flow is the rollback: no import rows means no Jobs. Cells never depend on an import having run.

Until step 4 ships, an operator can run the same tenant pipeline by hand: prepare and import under break-glass, with a ciphertext the tenant hands over. That keeps the privacy property, but it puts a person in the loop for every import, so it is a stopgap, not the product path.

## Open Questions

- The per-import size and member-count caps, and whether storage allowance can grow automatically for a declared import within a plan's limit. These are values; the mechanism does not change.
- The staging folder name (`_Imports/`), to be settled against the vault layout conventions when section 2 lands.
- Whether B2's CORS accepts the rule for the web app's origin as written. Task 3.6 runs a real preflight and part upload against a disposable prefix before section 4 relies on it.
