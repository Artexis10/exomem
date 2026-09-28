## Why

Exomem Cloud has no way in for notes people already have. A cell registers only the MCP endpoint and health routes, and `adopt_vault` is excluded from cells because there is "no upload-then-adopt staging flow". So the owner cannot move their vault into Cloud, and invited friends can only start empty. People need to bring their notes in, and once the notes are inside, Exomem can reorganise them with the user's intent.

The import path is also where privacy is easiest to lose. An archive of someone's notes is the most sensitive object Cloud will ever handle, so the path must keep plaintext out of the operator's hands by construction. The parts a confidential cell would later need to protect, minting the key, decrypting and unpacking, must already run inside the cell.

## What Changes

- **Owner restore, now.** A runbook imports the Cloud owner's existing Exomem vault into the owner's stopped cell. It applies to the owner's own vault only.
  - The vault is archived where it lives, age-encrypted to a one-time key minted on the node, and streamed to the node. Every intermediate machine only sees ciphertext.
  - A break-glass helper pod unpacks it into the cell's volume. It replaces `/data/vault`, keeps the cell's custody directory, and sets the old derived state aside so the cell rebuilds it.
  - The prior vault is kept until the restored cell answers recall and a backup completes, so the swap can be reversed.
- **Tenant self-serve import.** An invited tenant imports their own notes without the operator ever holding plaintext:
  - the cell mints the import key on its own volume, outside its backups;
  - the tenant's browser or CLI encrypts to it and uploads ciphertext straight to object storage, then reports what it uploaded through its signed-in session, so a substituted upload is caught;
  - a cell-scoped import Job decrypts and unpacks inside the cell under an `import` hold.
- **Staging and adoption.** A tenant import lands in a staging folder inside the vault, never over existing notes. Exomem's adoption flow then scans it and proposes how to file it with the user's intent. This lifts the Cloud exclusion of `adopt_vault`. A tenant can discard a staged import completely.
- **Limits and failure.** Imports are bounded by the cell's storage allowance and a per-import size cap. Archive members are validated (no links, no path escapes, no special files), and nothing is committed until the whole archive is proven complete. A failed import leaves the vault as it was, and the ciphertext and key are deleted when the import ends.

## Capabilities

### New Capabilities

- `cloud-vault-import`: covers how existing notes enter an Exomem Cloud cell:
  - encryption to a key the cell holds;
  - direct ciphertext upload, and cell-side decryption and unpacking under an import hold;
  - staging, adoption and discard for tenant imports, and replacement for the owner's vault restore;
  - archive validation, limits, failure behaviour and cleanup.

### Modified Capabilities

None among the canonical specs. The Cloud cell's tool-exclusion requirement is still in the active change `adopt-exomem-cloud-plain-cells`, and task 2.3 amends it there.

## Impact

- **Exomem (`src/exomem`):**
  - a validating unpacker, and cell commands that mint the import key, import, and discard;
  - a `cell-init` guard against initialising an empty vault in the middle of a restore;
  - `adopt_vault` becomes available on cells, through a `CLOUD_SURFACE_EXCLUSIONS` update;
  - an `exomem cloud import` CLI path for technical tenants;
  - an age implementation in the cloud image.
- **cellctl:**
  - a new `import` hold, with a deadline;
  - prepare, import and discard Jobs rendered on the cell image;
  - presigned part upload URLs scoped to one import object under the cell's own prefix, and completion of the upload with the tenant's reported ETags;
  - ciphertext deletion when the import ends, and a lifecycle rule for abandoned uploads.
- **Substrate (its own change):**
  - an import request table (contract C5), and `import` added to C1's hold kinds;
  - a "Bring your notes" flow on the Cloud home page that encrypts in the browser with an age implementation;
  - CORS on the Cloud bucket for direct uploads.
- **Infrastructure:** `age` in the node's base packages, for the owner restore.
- **Runbooks:** `docs/runbooks/cloud-operator-import.md` for the owner restore. The operator-access and export runbooks gain cross-references.
- **Privacy:** no new operator access to another person's plaintext. The import key stays on the tenant volume, outside its backups, under the same boundary as the vault itself. Confidential cells would still need an attested recipient and a client the operator does not serve (design, Risks).
