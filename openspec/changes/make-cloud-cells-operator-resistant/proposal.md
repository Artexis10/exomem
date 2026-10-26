## Why

Exomem Cloud cells can hold very private material. Today the operator, and anything that holds the operator's credentials, can read any cell with little effort:

- One CSI passphrase encrypts every cell volume. The backup master key and the per-cell restic passwords are cluster Secrets on the serving node.
- Every operator procedure, including the read-only checks, starts with a login to the node as an administrator with passwordless root. One command later, any vault's decrypted mount is readable.
- The exec guard on cells stops mistakes, not intent. The cluster audit log stays on the node, where root can rewrite it.

Automated agents now run many operator procedures. The privacy boundary therefore must not depend on the good behaviour of whoever holds the operator's credentials.

Strict end-to-end encryption does not fit, because the product is server-side search, recall and processing over plaintext. Hardware confidential computing (AMD SEV-SNP, Intel TDX) can make a running cell unreadable to the operator. On the current hosting it is not available, and elsewhere it raises the cost per cell (see design D8). This change makes access as hard as the current infrastructure allows, at no material extra infrastructure cost. It records the hardware step and the trigger to take it.

## What Changes

- **A per-cell key hierarchy.** Each cell gets one cell root key. The volume passphrase, the backup repository password and the media key bundle are wrapped by that key, not by a platform-wide key. Content scratch (temporary files, media job scratch, import and export staging) moves off unencrypted node disks onto the cell's volume or into memory. Existing cells move to new volumes through the relocation path. Deleting a cell destroys its live keys at once; the copies in platform backups expire by a date the platform shows.
- **An opt-in tenant lock.** A tenant can wrap their cell root key with their own key, from a recovery code and optionally a passkey. From then on, no platform copy of a key that decrypts its content is written anywhere, so such a key cannot travel through a Kubernetes Secret. The outer volume passphrase stays platform-held, because it opens only the inner layer's ciphertext. An existing cell converts with new keys, and earlier platform copies expire with backup retention. A crash or node failure locks the cell until the tenant unlocks it. A planned restart hands the key over without the tenant. A locked cell never sleeps.
- **An access record.** Access events go off the node into object storage where no user can delete them within the retention period. These events are exec, attach, port-forward, ephemeral containers, Secret reads, admission and RBAC changes, and node shell sessions. A heartbeat makes a stopped shipper visible as a gap. The record retains what its sources submit; root on a node can still suppress events before they leave it.
- **A tenant access log.** Each tenant sees the operator access that the record received for their cell, and every gap in the record, served by the control plane even while the cell is locked.
- **Three access tiers.**
  - Routine checks use a read-only identity from the operator's own machine, with no node login.
  - Image rollouts use a deploy identity that can change only container images, and only to images that the release workflow attested from a release tag on `main`, which only the release App can create, move or delete.
  - Every operator and recovery credential with admin capability needs a physical touch on an authenticator that the operator holds: node shells, break-glass, structural platform changes, and the provider, datastore, snapshot, Terraform and escrow credentials. The release publisher's run-scoped token and the credentials that running services hold are exempt within stated boundaries.
- **Truthful claims for each mode.** The privacy copy may claim only the properties in force for the cell's mode, and states each limit beside the claim.
- **Confidential cells, parked.** The design records the direction for per-cell SEV-SNP cells, the trigger that starts that work, and an evidence task on rentable hardware.

## Capabilities

### New Capabilities

- `cloud-cell-key-custody`: the per-cell key hierarchy, key expiry on deletion, and the opt-in tenant lock.

### Modified Capabilities

- `cloud-operator-access`:
  - routine access is off-node;
  - every admin-tier credential, node shells and break-glass included, needs the operator's presence, and break-glass use goes to the access record;
  - new requirements for the access record, the tenant access log and the deploy identity;
  - the privacy-copy requirement gains per-mode claims and disclosures.

## Impact

- **cellctl:** cell root key minting, wrapping and expiry; per-cell volume Secrets; relocation for migration and lock conversion; the tenant-lock key channel and handover; the tenant access log derivation.
- **Gateway:** the `CELL_LOCKED` answer from recorded cell state.
- **Cell runtime:** the locked state; paused background work that reports `locked`; its half of the handover.
- **Platform chart:** the per-cell StorageClass Secret template; the audit shipper; RBAC for the routine and deploy identities; the image-only admission rule and the release-attestation verifier.
- **Ansible:** node SSH through FIDO2 `-sk` keys, node session events, and the audit policy.
- **Terraform:** one B2 bucket with Object Lock in compliance mode.
- **Admin credential custody:** presence-gated encryption of the provider, datastore, snapshot, Terraform, escrow and registry credentials.
- **Control plane (separate repository):** the tenant unlock flow and recovery code, the tenant access log view, and the privacy copy.
- **Other changes:**
  - `add-cloud-media-store` wraps its media key bundle under the backup master key. This change re-roots that bundle under the cell root key.
  - `move-cloud-cells-to-local-storage` encrypts at the node level and lists per-tenant keys as a non-goal. Its cutover must add a per-volume LUKS2 layer first.
  - `add-cloud-cell-sleep` lets idle cells sleep. A tenant-locked cell never sleeps.

**Not changed:** tenant authentication, cell isolation between tenants, the backup format, and the hosting provider.
