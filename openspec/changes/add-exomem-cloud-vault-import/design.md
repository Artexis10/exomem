## Context

See proposal.md for why. The constraints this design works within, all from the Cloud cell design (`adopt-exomem-cloud-plain-cells`) and the operator-access boundary (`cloud-operator-access`):

- **Cell runtime.** It registers only MCP and health routes. It has no egress, not even DNS, and no control-plane access. Its volume holds `/data/vault` (the vault) and `/data/host` (the custody root: identity, auth and writer-lease state, owner-only modes).
- **Backup and restore Jobs.** They run on the cell image in the cell's namespace, with object-storage egress only. They authenticate as the cell's prefix-restricted B2 key.
- **cellctl.** It is row-driven, with no API. It holds each cell's B2 key while it reconciles, and it records holds (`backup`, `upgrade`, `restore`) on the row.
- **Operators.** The everyday identity cannot exec into cell or scratch namespaces. Break-glass is minted per task and audited.
- **`cell-init`.**
  - It creates the vault only when the volume has none, then migrates state.
  - A vault at governance schema v4 either refuses writes or refuses recall in a standalone cell.
  - `init_vault` binds a per-vault state key to the vault path.
- **`adopt_vault`.** It scans a vault subtree and copies or compiles the chosen files into the governed Knowledge Base layer, keeping original paths and hashes. It is excluded from cells until an upload-then-adopt path exists.

## Goals / Non-Goals

**Goals:**
- Import paths where no operator-controlled component holds the import key or the plaintext, other than the cell's own volume. That is the same boundary as the vault itself.
- A design that moves unchanged into confidential cells, because key minting, decryption and unpacking all happen in the cell's own pods on its own volume.
- The owner's vault imported first, by an operator runbook that needs no new cellctl or web code.

**Non-Goals:**
- Converting other tools' export formats (Notion, Evernote, Roam). Markdown and plain files come in as they are, and adoption works on them. Converters are a later change.
- Continuous sync from a desktop vault. This is a one-shot import.
- Changing the operator trust model. Confidential cells are a separate change.

## Decisions

### D1. Two paths, one pipeline

There are two paths:

- **Tenant import.** It lands in a staging folder, for adoption.
- **Operator restore.** It replaces the vault of an existing Exomem user, starting with the owner.

Both share the same pipeline: encryption to a held key, direct ciphertext transfer, streaming decrypt-and-unpack with member validation (D6), and a stopped cell. They differ only in where members land and in who mints the key.

*Rejected: one mode that always replaces the vault.* A friend's notes are rarely an Exomem vault. Writing them over a cell that may already hold notes loses data, and it skips the adoption step that files them with intent.

### D2. The cell mints the tenant's import key, and only the public half leaves

cellctl runs a short **prepare Job** on the cell image in the cell's namespace:

1. It generates an age X25519 identity.
2. It writes the identity to `/data/host/imports/<import-id>/identity`, mode `0600`, owned by the cell user.
3. It writes the public recipient to the container's termination message.

cellctl reads the recipient from the pod status and records it on the import row. The termination message is a standard Kubernetes channel for a small status value. It is the only thing that leaves the pod, and it holds no secret.

The volume is ReadWriteOnce and the cluster has a single node, so the prepare Job can mount it while the cell runs. It writes only under `/data/host/imports`.

*Rejected: cellctl mints the key.* The key would then live in the controller's memory and, wrapped, in the database. That is a second place holding a tenant secret, and it does not move into a confidential cell.

*Rejected: an MCP tool on the cell.* The runtime cannot tell the control plane that an import exists, and the web flow cannot call MCP without holding a tenant token.

The operator restore instead mints a one-time key on the node in `/dev/shm`. The owner restore happens before the prepare Job exists, and the operator is the tenant.

### D3. Direct multipart upload with presigned, single-object URLs

For each import, cellctl:

1. creates an S3 multipart upload for `cells/<cell-id>/imports/<import-id>.age`, using the cell's own key;
2. presigns one PUT URL per part and one URL to complete the upload;
3. writes those URLs to the import row with a 24-hour expiry.

The client uploads the parts and completes the upload directly. The web app and the gateway never carry the bytes.

A presigned URL authorises exactly one operation on one object, so a leaked URL can at most replace that import's ciphertext. Such an archive then fails decryption (D2), so it cannot inject content.

The Cloud bucket gains a CORS rule allowing `PUT` from the web app's origin and exposing `ETag`.

*Rejected: upload through the gateway or the web app.* It puts plaintext-sized request bodies through operator-controlled code. It also runs into Vercel's request limits for multi-gigabyte archives.

### D4. Contract C5: the import request row

A new table, `exomem_cloud_imports`, joins Substrate and cellctl the same way `exomem_cloud_cells` does (C1). Column grants follow the same split:

- **Substrate writes:**
  - the request: `cell_id`, `kind`, `declared_bytes`, `declared_members`;
  - the transitions `requested`, `uploaded` and `cancelled`.
- **cellctl writes:**
  - `recipient`, `upload_id`, `part_urls`, `complete_url`, `urls_expire_at`;
  - the transitions `key_ready`, `importing`, `done` and `failed`;
  - `error_code`.

Other rules:

- Nothing in the row is plaintext content.
- One import per cell may be open at a time.
- The row outlives the import as a content-free record.
- Expired or cancelled imports are cleaned up by cellctl (D8).

### D5. An `import` hold stops the cell for the import's duration

When a row reaches `uploaded`, cellctl:

1. takes an `import` hold, which stops the runtime, records the hold on the row, and resumes it after a restart;
2. runs the **import Job** on the cell image, with object-storage egress;
3. releases the hold and starts the cell again, on success or failure.

The import Job streams the object through `age -d` (the identity comes from the volume) into the validating unpacker (D6).

*Rejected: importing while the cell serves.* The runtime would index half-written files, and the writer lease assumes a single writer. A one-time import can afford minutes of downtime.

### D6. A validating streaming unpacker writes to a staging directory, then moves it once

The unpacker reads the tar stream member by member. It refuses the whole import, before writing, on:

- links, devices or other special entries;
- absolute paths or `..` components;
- names invalid on the filesystem;
- going over the declared or maximum size or member count.

It writes into `/data/.import-<import-id>` on the same volume, never into the vault directly. Where the result goes next depends on the path:

- **Tenant import.** One `rename` moves the staging directory to `/data/vault/_Imports/<date>-<import-id>/`. Nothing existing is touched, and a failure only removes the staging directory.
- **Operator restore.** The current vault is renamed aside to `/data/.vault-prior-<hex>`, and the staging directory is renamed to `/data/vault`. Derived state keyed to the vault is removed (see tasks), so the runtime rebuilds it. The prior vault is kept until the restored cell has passed recall and a backup, then deleted.

Validation happens before any member is written because a stream cannot be rolled back after a partial write into the vault. Staging on the same filesystem makes the final step atomic.

### D7. Adoption lifts the `adopt_vault` exclusion, for staged imports only

`adopt_vault` becomes available on cloud cells. Its `path` must name a folder under `_Imports/`, and any other path is refused with a typed error. The tenant's assistant scans the folder, and Adoption Studio proposes how to file it. Filing uses governed writes, with provenance to the original path and hash.

The `CLOUD_SURFACE_EXCLUSIONS` entry for `adopt_vault` is replaced by this path restriction. `transfer_artifact`, `process_media` and `read_media` stay excluded.

### D8. Cleanup and failure

When an import ends, whether `done`, `failed`, `cancelled` or `expired`, cellctl:

- deletes the ciphertext object (every version, as in verified deletion);
- aborts any open multipart upload;
- runs a cleanup Job that deletes `/data/host/imports/<import-id>/` and any staging directory.

A failed import leaves the vault exactly as it was. Error codes are typed and content-free: `ARCHIVE_UNDECRYPTABLE`, `ARCHIVE_MEMBER_REFUSED`, `ARCHIVE_TOO_LARGE`, `STORAGE_ALLOWANCE_EXCEEDED`, `IMPORT_EXPIRED`. Import logs carry counts, sizes, durations and codes only.

### D9. The owner restore runs before any of the above exists

The owner's vault comes in through `docs/runbooks/cloud-operator-import.md`. It uses only what exists today: break-glass, a scratch namespace, the cell image, and the machine connection from `herdr-peer-sync`. The steps:

1. **Check compatibility.** Read the vault's governance schema on the source machine, and stop if a standalone cell cannot serve it.
2. **Mint the one-time key.** `age-keygen` on the node, into `/dev/shm`.
3. **Stream.** On the source machine, tar the vault, excluding derived state, and pipe it through `age -r <recipient>`. The ciphertext streams through the operator's machine into a node file. Plaintext never touches the intermediate machine's disk.
4. **Rehearse in scratch.** A scratch namespace gets a fresh volume and the cell's rendered resources, as in the export runbook. The unpacker (break-glass exec, `age -d` on the node piped into it) fills it. The cell image starts read-only on it and answers recall for a known note, reporting only a hit count. Then the scratch namespace is deleted.
5. **Swap for real.** Stop the cell: `desired_state = stopped` on its row, as an operator transaction. Unpack and swap as in D6, then set the cell running again. The prior vault stays until verification.
6. **Verify.** Recall works through the tenant's own connector, and the cell's next backup succeeds. Only then is the prior vault deleted, along with the ciphertext and `/dev/shm` key.

## Risks / Trade-offs

- **The import key sits on the tenant volume, which the operator can read deliberately.**
  - This is the same boundary as the vault, and the spec's privacy requirement already discloses it.
  - Confidential cells close it for both at once.
- **Browsers encrypting multi-gigabyte folders.**
  - Mitigation: streaming tar plus age encryption with bounded memory, and resumable multipart parts.
  - `exomem cloud import` is the path for very large vaults.
- **Removing too little or too much derived state in a restore.**
  - Mitigation: a task inventories every path keyed to the vault before the runbook is written.
  - The scratch rehearsal must answer recall before the real swap.
- **A vault at governance schema v4.**
  - Mitigation: refuse it before touching the cell.
  - A detach step is a follow-up if the owner's vault needs one; the first runbook task checks.
- **Presigned URLs on the import row.**
  - Each is write-only for one object, and expires.
  - The row readers are Substrate and cellctl, which already hold stronger credentials.
- **Downtime during an import hold.**
  - A one-time import stops the cell for minutes.
  - The tenant starts it and sees its state.

## Migration Plan

1. **Owner restore by runbook (section 1 of tasks).** It changes no product code and uses break-glass on the node.
2. **Cell-side commands and the adoption path (section 2).** These ship in a normal Exomem release, and cells pick them up through the rollout.
3. **cellctl import support and C5 (section 3).** This ships with the platform chart. The Substrate migration for C5 lands first, because cellctl only reads the table.
4. **Substrate web flow (section 4).** This is its own Substrate change. Tenant imports switch on when it deploys.

Each step is additive. If step 3 or 4 misbehaves, turning off the Substrate flow is the rollback: no import rows means no Jobs. Cells never depend on an import having run.

## Open Questions

- The per-import size and member-count caps, and whether storage allowance can grow automatically for a declared import within a plan's limit. These are values; the mechanism does not change.
- The staging folder name (`_Imports/`), to be settled against the vault layout conventions when section 2 lands.
