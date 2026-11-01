## Why

The Cloud controller, `cellctl`, holds enough to read or destroy every tenant's data. If an attacker gets code running inside it, through a bug, a dependency or a bad image, nothing else has to go wrong:

- It holds the platform master keys, so it can unwrap every cell's backup repository password and object-store key, and read every backup directly from object storage.
- It holds the one fleet-wide key that derives every cell's request bearer. The gateway holds the same key.
- It holds an object-store key that can create keys. The provider limits neither the capabilities nor the buckets of a created key, so `cellctl` can create a key that reads, deletes or reconfigures any bucket in the provider account.
- It writes every cell's StatefulSet and Job. The admission policy pins images, volumes and placement, but not the command, arguments or environment. So `cellctl` can run any command from the cell image against any cell's volume.
- It presigns every export and import URL. The cell checks only that a URL starts with `https://`, so `cellctl` can send a cell's data to any address.
- A cell Job may reach any public address on TCP 443.

The source code is public. The design must therefore hold against an attacker who has read every line. It may rest only on keys that the attacker does not hold and on checks that the attacker cannot skip.

`make-cloud-cells-operator-resistant` hardens the platform against the operator's credentials, and treats `cellctl`'s wrapping keys as service credentials inside the runtime. This change adds the adversary that it leaves out: a compromised controller.

## What Changes

- **Cells trust only signed requests.** A cell accepts a request only with a valid gateway proof, signed by the gateway's Ed25519 key, which the cell already verifies. The fleet-wide bearer key is retired. Neither `cellctl` nor the gateway then holds a key that derives a credential a cell accepts.
- **The admission policy pins every trust anchor that a cell reads.** That covers the verifying keys, the object-store endpoint and bucket, and the escrow recipient. A Helm release renders their values into the admission policy, which `cellctl` cannot change. `cellctl` therefore cannot swap in its own key.
- **Cell workloads run a closed pod template.** The admission policy accepts a cell StatefulSet or Job only when the whole pod template has an allowlisted shape. That covers:
  - the fixed `exomem` subcommand;
  - no lifecycle hooks and no exec probes;
  - exact volume mounts;
  - allowlisted environment names and Secret keys;
  - no DNS override.

  Backup and restore move from joined shell strings onto `exomem cell-backup` and `exomem cell-restore`.
- **Data moves only to destinations that a signed grant names.** Substrate signs a grant when it creates an export or import request. The grant binds the tenant, the cell, the operation, its ID, the object key, and for an import the size and SHA-256. The cell verifies the grant. It then checks that each presigned URL points at the pinned endpoint and bucket, under the object key that the grant names. A backup goes only to the cell's own repository, which the cell derives from its own cell ID and the pinned endpoint.
- **Owner exports and import uploads are encrypted to recipients that `cellctl` cannot replace.**
  - An export is encrypted to a recipient that the tenant's browser generates, and the signed grant names it.
  - An import uses the cell's recipient. The gateway fetches it from the cell, on a path that excludes `cellctl`.
- **The controller cannot decrypt backups.** Each cell generates its own backup repository password, keeps it on its encrypted volume, and writes an escrow copy. `cellctl` then holds no key that decrypts a backup.
  - **Standard mode:** the escrow copy opens only with the operator's presence-gated key.
  - **Opt-in tenant-locked mode:** it opens only with the tenant's own key, so the operator cannot decrypt that cell's backups either.
- **Exports cannot be forged.** The cell reports each archive's digest through the gateway, and the tenant's browser checks it before it accepts the download.
- **One key custodian creates object-store keys.** A small component outside `cellctl`'s reach holds the only key that can create keys, and it creates only the per-cell key shape. `cellctl` keeps a key that can only list files. A key watcher outside the cluster alerts on any other key and on any change to the backup bucket's lock settings. Cell data later moves to a separate provider account in the same billing group, at the backup-lock migration.
- **Attack signals alert through a path that excludes the controller.** These signals are an admission denial for `cellctl`'s identity, a grant check that fails in a cell, a Secret read by an unexpected identity, and a Job of an unknown kind. The access record pipeline of `make-cloud-cells-operator-resistant` evaluates them and sends each alert to the receiver.
- **Job egress narrows to object storage.** This is a later phase: once the command pin and the grants are in force, a cell Job's egress goes through a platform egress proxy that allows only the pinned object-store host.

## Capabilities

### New Capabilities

- `cloud-controller-containment`: what a compromised controller can no longer do, and the checks that a cell and the admission policy make without trusting the controller.

### Modified Capabilities

None. `cloud-operator-access` keeps its privacy claims. The operator still holds the volume key and the escrow key, and could deliberately read content.

## Impact

- **Cell runtime:**
  - a mandatory request proof;
  - the grant verifier;
  - URL destination checks;
  - `exomem cell-backup` and `exomem cell-restore`;
  - cell-held backup passwords and escrow writing.
- **cellctl:**
  - stops minting and unwrapping backup passwords and stops rendering the bearer;
  - loses its key-creating object-store key;
  - relays grants from request rows to Jobs;
  - renders Jobs as `exomem` subcommands.
- **Key custody:** the key custodian (a new small component) and the key watcher, which runs outside the cluster.
- **Platform chart:**
  - admission rules that pin the command, environment names and trust anchor values;
  - the egress proxy, in the later phase.
- **Substrate (separate repository):**
  - signs the export and import grants;
  - the gateway signs every cell request;
  - the gateway stops deriving bearers.
- **Coordination with `make-cloud-cells-operator-resistant`:**
  - Its cell root key keeps wrapping the volume passphrase and the media keys.
  - The backup repository password leaves that hierarchy for the cell-held password with escrow (design D5).
  - The attack-signal alerts build on its access record (design D7).

**Not changed:** tenant authentication, the privacy claims, the backup format, the hosting provider, and the operator's ability to read content deliberately.
