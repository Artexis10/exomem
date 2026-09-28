## Why

Exomem Cloud has no way in for notes people already have. A cell registers only the MCP endpoint and health routes, and `adopt_vault` is excluded from cells because there is "no upload-then-adopt staging flow". So the owner cannot move his vault into Cloud, and invited friends can only start empty. People need to bring their notes in, and once the notes are inside, Exomem can reorganise them with the user's intent.

The import path is also where privacy is easiest to lose. An archive of someone's notes is the most sensitive object Cloud will ever handle, so the path must keep plaintext out of the operator's hands by construction, and be ready for confidential cells later.

## What Changes

- **Operator import, now.** A runbook imports an existing Exomem vault into a stopped cell, for the owner's own vault first.
  - The vault is archived where it lives, age-encrypted to a one-time key minted on the node, and streamed to the node without landing in plaintext on any intermediate machine.
  - A break-glass helper pod unpacks it into the cell's volume. It replaces `/data/vault`, keeps the cell's custody root `/data/host`, and removes derived state so the cell rebuilds it.
  - It is rehearsed in a scratch namespace first, and a backup follows the swap.
- **Tenant self-serve import.** An invited tenant imports their own notes without the operator ever holding plaintext:
  - the cell mints the import key on its own volume;
  - the tenant's browser or CLI encrypts to it and uploads ciphertext straight to object storage;
  - a cell-scoped import Job decrypts and unpacks inside the cell under an `import` hold.
- **Staging and adoption.** A tenant import lands in a staging folder inside the vault, never over existing notes. Exomem's adoption flow then scans it and proposes how to file it with the user's intent. This lifts the Cloud exclusion of `adopt_vault` for staged imports.
- **Limits and failure.** Imports are bounded by the cell's storage allowance and a per-import size cap. Archive members are validated (no links, no path escapes, no special files). A failed import leaves the vault as it was, and the ciphertext is deleted when the import ends.

## Capabilities

### New Capabilities

- `cloud-vault-import`: covers how existing notes enter an Exomem Cloud cell:
  - encryption to a key the cell holds;
  - direct ciphertext upload, and cell-side decryption and unpacking under an import hold;
  - staging and adoption for tenant imports, and replacement for an operator-run vault restore;
  - archive validation, limits, failure behaviour and cleanup.

### Modified Capabilities

None among the canonical specs. The Cloud cell's tool-exclusion requirement is still in the active change `adopt-exomem-cloud-plain-cells`, and task 2.3 amends it there.

## Impact

- **Exomem (`src/exomem`):**
  - a cell-side import command that mints the import key and unpacks a decrypted archive into staging;
  - `adopt_vault` becomes available on cells, for staged imports only;
  - a `CLOUD_SURFACE_EXCLUSIONS` update;
  - an `exomem cloud import` CLI path for technical tenants.
- **cellctl:**
  - a new `import` hold;
  - an import Job rendered on the cell image with object-storage egress only;
  - presigned multipart upload URLs scoped to one import object under the cell's own prefix;
  - ciphertext deletion when the import ends.
- **Substrate (its own change):**
  - an import request table (contract C5);
  - a "Bring your notes" flow on the Cloud home page that encrypts in the browser with an age implementation;
  - CORS on the Cloud bucket for direct uploads.
- **Runbooks:** `docs/runbooks/cloud-operator-import.md` for the operator path. The operator-access and export runbooks gain cross-references.
- **Privacy:** no new operator access to plaintext. The import key stays on the tenant volume, under the same boundary as the vault itself, so moving cells into confidential computing later moves the key with them.
