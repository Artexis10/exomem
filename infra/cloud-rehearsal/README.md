<!-- authority:non-specification -->

# Exomem Cloud local rehearsal (P3)

`adopt-exomem-cloud-plain-cells` tasks 5.1–5.3. This is one command. It
stands up the whole Exomem Cloud stack on one machine and runs the twelve P3
steps from design.md in order. It then writes a JSON report with the 5.3
measurements and tears everything down.

It is the gate for P4: nothing is deployed to the real node until a valid
report says `"gates_node": true`.

```
cd infra/cloud-rehearsal
uv run --frozen exomem-cloud-rehearsal run --report cloud-rehearsal-report.json
```

It needs Docker (privileged containers), Node 24 with npm, git, and the
pinned `uv`. It needs egress to GitHub, Docker Hub, the npm registry, PyPI and
Hugging Face for the builds. The stack itself has no egress.

The `Cloud rehearsal` workflow runs the same command on a GitHub runner.
Trigger it by hand before P4.

The exit code is 0 when the report gates the node, 1 when any step or target
fails, and 2 when the stack could not be stood up.

## What runs

| Piece | How it runs |
|---|---|
| Kubernetes | The pinned K3s image (the same gate contract the cellctl live suite reads), privileged in Docker. Traefik, servicelb and metrics-server are off, and secrets are encrypted at rest. |
| Control database | PostgreSQL 17 with the four control-database roles. It runs Substrate's real `scripts/migrate.ts`, which also applies Substrate's real `scripts/exomem-cloud-grants.sql`. |
| Substrate | Cloned at a pinned commit (`substrate.SUBSTRATE_COMMIT`) and built with `next build`. `next start` runs in the gateway's pinned Node image on an `--internal` Docker network, so it can reach PostgreSQL and nothing else. A Traefik edge terminates TLS for `substrate.rehearsal.test`. |
| Gateway | Built from Substrate's `Dockerfile.exomem-gateway`. It is deployed by the platform chart's `cloud-gateway.yaml` behind a Traefik stand-in that terminates TLS for `mcp.rehearsal.test`. |
| cellctl | Deployed by the platform chart's `cellctl.yaml`, with its RBAC, admission policy and NetworkPolicies. It runs the real `run_loop`. |
| Cell | Built from this repository's `Dockerfile --target cloud`. The run builds three variants. `v2` is the same source rebuilt with a later build time, so it has a new digest. `broken` runs `cell-init` for real but never serves. |
| Object storage | An S3 double (Versity Gateway) on the Docker network. The real backup and restore Jobs run restic against it. |

A throwaway CA, made per run, signs both hostnames. Every client resolves
them to local ports and trusts only that CA.

## Faked or injected, never real

- **Email.** Substrate stores every emailed secret (invite, deletion token) only as an unsalted SHA-256 digest. The rehearsal mints the secret, seeds the digest the way the email path would, and uses the secret as the recipient would.
- **Paddle.** Checkout binding is seeded. Activation, past-due, recovery and cancellation are real signed webhooks, signed with the run's own secret.
- **B2 key management and Hetzner.** Both use cellctl's own doubles. Every per-cell key is the S3 double's one credential. D10's absence proof lists the live S3 double.
- **Neon and Vercel.** They are not involved: PostgreSQL is local and Substrate runs `next start`.

## Steps

1. **Invite.**
2. **Provision** a paid tenant (A): invite redemption, checkout, then a `subscription.activated` webhook. Timed until cellctl observes the cell running and ready. The target is under 3 minutes.
3. **OAuth MCP `tools/list`.** The MCP SDK's OAuth client runs gateway 401, discovery, PKCE with `resource`, consent and token. Warm `initialize` and `tools/list` are timed.
4. **Capture** (`capture_source`).
5. **Paraphrased cited recall** (`ask_memory`).
6. **Governed write (`remember`) after a pod kill.** Owner-only modes are checked.
7. **Upgrade and recall.** Uses the owner's release route. The time includes the pre-upgrade backup.
8. **Forced canary failure.** The rollout pauses with `UPGRADE_READINESS_TIMEOUT` and the pre-upgrade snapshot is restored onto the previous image. The step then checks that a write made before the attempt survives.
9. **Read-only mode** through `subscription.past_due`, then recovery.
10. **Second-tenant denial.** A complimentary tenant (B) is added, then the step checks:
    - no cross-recall between tenants;
    - selector headers are refused;
    - forged and anonymous bearers are refused;
    - no network path from B's cell to A's.
11. **Backup and scratch restore.** A nightly backup runs for B, then the operator export path restores it into a scratch namespace. That restore must answer recall, report the same `governance-schema status` as its source, and accept a governed write.
12. **Deletion with absence proofs.** Account deletion runs through Substrate. The step then checks that each of these is gone:
    - the namespace;
    - the PVs;
    - the backup objects;
    - the wrapped keys;
    - the token.

A post-check scans the gateway, cellctl and cell logs for the run's distinctive phrases.

## Report

`schema: exomem-cloud-rehearsal-report-v1`. It records:
- the pinned inputs and image digests;
- every host adaptation and chart overlay;
- each step's status, seconds, evidence and failure;
- the 5.3 measurements against their targets;
- the cross-lane defects found, with their owners.

It never contains a secret.

The run is marked not valid, and cannot gate the node, when the cell image was supplied with `--cell-image` or when only some steps ran (`--steps`).

## Host adaptations

K3s-in-Docker adaptations are detected, applied only where needed, and each one is listed in the report:
- cgroup v1 hosts;
- hosts that withhold CAP_SYS_RESOURCE;
- image GC on a shared disk;
- system images imported instead of pulled.

`--gateway-build host` is for machines whose `docker build` cannot reach the npm registry with a trusted CA. It assembles the gateway image's final stage from a host build, and the report records that it did.
