## Why

Exomem Cloud cells can hold very private material. Today the operator, and anything that holds the operator's credentials, can read any cell with little effort:

- One CSI passphrase encrypts every cell volume. The backup master key and the per-cell restic passwords are cluster Secrets on the serving node.
- Every operator procedure, including the read-only checks, starts with a login to the node as an administrator with passwordless root. One command later, any vault's decrypted mount is readable.
- The exec guard on cells stops mistakes, not intent. The cluster audit log stays on the node, where root can rewrite it.

Automated agents now run many operator procedures. The privacy boundary therefore must not depend on the good behaviour of whoever holds the operator's credentials.

Strict end-to-end encryption does not fit, because the product is server-side search, recall and processing over plaintext. Hardware confidential computing (AMD SEV-SNP, Intel TDX) can make a running cell unreadable to the operator. On the current hosting it is not available, and elsewhere it raises the cost per cell (see design D8). This change makes access as hard as the current infrastructure allows, at no material extra infrastructure cost. It records the hardware step and the trigger to take it.

## What Changes

- **A per-cell key hierarchy.** Each cell gets one cell root key. The volume passphrase, the backup repository password and the media key bundle are wrapped by that key, not by a platform-wide key. Deleting a cell destroys its keys, so its backups and stored media become unreadable everywhere.
- **An opt-in tenant lock.** A tenant can wrap their cell root key with their own key, from a recovery code and optionally a passkey. The platform then holds no copy of the key at rest. A crash or node failure locks the cell until the tenant unlocks it. A planned restart hands the key over without the tenant.
- **A locked access record.** Access events go off the node into object storage that nobody can delete within the retention period. These events are exec, attach, port-forward, ephemeral containers, Secret reads, admission and RBAC changes, and node shell sessions. A heartbeat makes a stopped record visible as a gap.
- **A tenant access log.** Each tenant sees every operator access that touches their cell, and every gap in the record.
- **A three-way access split.**
  - Routine checks use a read-only identity from the operator's own machine, with no node login.
  - Image rollouts use a deploy identity that can change only container images, and only to release-built digests.
  - Content paths need a physical touch on an authenticator that the operator holds. Content paths are node shells, break-glass and structural platform changes.
- **Truthful claims for each mode.** The privacy copy may claim only the properties in force for the cell's mode, and states each limit beside the claim.
- **Confidential cells, parked.** The design records the direction for per-cell SEV-SNP cells, the trigger that starts that work, and an evidence task on rentable hardware.

## Capabilities

### New Capabilities

- `cloud-cell-key-custody`: the per-cell key hierarchy, key destruction on deletion, and the opt-in tenant lock.

### Modified Capabilities

- `cloud-operator-access`:
  - routine access is off-node;
  - break-glass and node shells need the operator's presence and land in the locked access record;
  - new requirements for the locked record, the tenant access log and the deploy identity;
  - the privacy-copy requirement gains per-mode claims and disclosures.

## Impact

- **cellctl:** cell root key minting, wrapping and destruction; per-cell volume Secrets; the transient unlock Secret and the key handover; the deploy identity's admission rule; the tenant access log feed.
- **Cell runtime:** the locked state, with a coded `CELL_LOCKED` answer; the paused background work that reports `locked`; the handover endpoint.
- **Platform chart:** the per-cell StorageClass Secret template; the audit shipper; RBAC for the routine and deploy identities; the admission rule for image-only updates.
- **Ansible:** node SSH through short-lived certificates or FIDO2 keys, node session events, and the audit policy.
- **Terraform:** one B2 bucket with Object Lock in compliance mode.
- **Control plane (separate repository):** the tenant unlock flow and recovery code, the tenant access log view, and the privacy copy.
- **Other changes:**
  - `add-cloud-media-store` wraps its media key bundle under the backup master key. This change re-roots that bundle under the cell root key.
  - `move-cloud-cells-to-local-storage` encrypts at the node level and lists per-tenant keys as a non-goal. Its cutover must adopt per-volume LUKS2 first.
  - `add-cloud-cell-sleep` lets idle cells sleep. A tenant-locked cell does not sleep unless its tenant accepts waking locked.

**Not changed:** tenant authentication, cell isolation between tenants, the backup format, and the hosting provider.
