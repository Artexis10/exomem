## Why

Exomem Hosted has spent more than a month failing to admit a single user. More than twenty launch attempts produced no working cell, and every failure was in the protocol layer wrapped around the cell, not in the runtime or the infrastructure-as-code. That layer includes:

- provisioner-owned custody Secrets and attestation windows;
- activation tuples and acknowledgements, and governance migration Jobs;
- fenced checkpoint phases;
- contract candidates, cohorts and promotion;
- deployment locks and CEL pod-shape pins.

The hosted-only code measured about 400k lines across both repositories, for a fleet of zero cells. Repairing a read-only `control.json` alone added 17k lines.

The standalone runtime already does the hard part in production. It serves remote MCP with governed writes, its own custody and its own authentication, every day. Exomem Cloud runs exactly that runtime once per tenant, and adds only admission, routing and lifecycle.

## What Changes

- **Cloud cell mode.** A cell is standalone Exomem in a pod. It owns its custody on its own writable volume, runs offline state and governance migrations in a same-image init container before the server starts, and serves MCP itself.
  - It accepts one gateway-presented bearer.
  - It publishes the product tool surface minus a short list of technically broken exclusions.
  - It redacts content from logs and registers no REST or transfer routes.
- **cellctl.** A small in-cluster controller with no database of its own replaces the hosted provisioner for Cloud.
  - It reads desired cell rows from the control database and converges them with Kubernetes server-side apply.
  - It writes observed state back.
  - Waiting is normal. Only an identity conflict stops a cell.
- **Plain, hardened cell manifests.** Namespace with Pod Security `restricted`, quota, default-deny network policy, encrypted volume, one-replica StatefulSet and Service. They contain no custody Secrets. cellctl drives encrypted backups with the cell stopped, and one admission policy confines cellctl's own writes.
- **Releases.** A release is one image digest in a control-database setting. cellctl rolls cells one at a time, canary first, with a backup before each attempt. A canary that fails readiness is restored from that backup onto its previous image, and the rollout pauses.
- **Direct TLS ingress.** Our own Traefik terminates TLS with ACME certificates for the MCP hostname, so no third party terminates vault traffic. `cloudflared` is not in the Cloud data path.
- **Own control database.** Postgres on a separate small Hetzner server, with PgBouncer, verify-full TLS, least-privilege roles and point-in-time backups to B2, replaces Neon. It lives in the same Terraform and Ansible tree.
- **Deletion.** Deleting a tenant removes its namespace, volume and backups, and destroys its wrapped backup key.
- **Supersession.** The hosted trust-chain changes listed in design.md are superseded. Their code and canonical specs are retired in this change's final phase, after owner acceptance.

## Capabilities

### New Capabilities

- `cloud-cell`: The Exomem Cloud tenant cell. This covers runtime mode, controller, manifests, releases, ingress, backups, deletion and the control database it depends on.

### Modified Capabilities

None in this change. The retirement phase removes the superseded `hosted-*` capabilities together with their code, so that no canonical requirement describes deleted behaviour.

## Impact

- **Runtime:** a small cloud-mode seam in `server.py`, `server_auth.py`, `privacy_log.py` and `commands.py`, and a new `cloud` Dockerfile target.
- **Infrastructure:**
  - new `infra/cellctl/`;
  - new cell and gateway entries in the platform chart;
  - a Traefik ACME configuration;
  - Terraform for a control server and a private network;
  - an Ansible Postgres role.
- **Delivery:** a CI job that publishes the cell image by digest.
- **Companion Substrate change `adopt-exomem-cloud-plain-cells`:** owns admission, the OAuth authorization server, the pass-through gateway, the control-database schema and the database driver change.
- **Existing platform:** the running platform (Helm revision 65, zero cells) stays untouched until cutover and is removed in the retirement phase.
- **Migration preparation:** preserve the existing personal service and endpoint while preparing parallel hostname validation and rollback. Put Cloud key-management credentials in a dedicated backup account. Production activation is a separate checkpoint.
- **Out of scope:** billing semantics, the website, the Endstate products, and changes to the local standalone runtime, vault or custody.
