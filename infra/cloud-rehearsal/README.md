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

Exit codes:
- **0:** the report gates the node.
- **1:** the run recorded product findings (a failed step or a missed gating target).
- **2:** the rehearsal itself failed. Either a stage could not be stood up, or a step raised something other than a recorded finding.

`--harness-check` turns 1 into 0. Pull-request CI uses it, because there the question is whether the harness works, and the findings are in the report and the job summary. A manual dispatch of the workflow is the strict node gate.

With `--harness-check`, a failing step passes the check only when `known-findings.json` lists that step and its failure message contains the listed text. Every other failure exits 2 as a regression in the rehearsal. That includes a blocked step, an unlisted step, or a listed step failing for a different reason. A known finding whose step now passes is reported, so the baseline can be pruned.

`gates_node` is true only when all of the following hold:
- every step passed;
- every gating target was met (capture p95 is reported but informational on a CI runner: its 1 s target is re-measured on the node at P4 owner acceptance);
- the run is valid;
- no cross-lane defect was recorded;
- no distinctive phrase reached a log;
- every row's `ready` matches its pod.

`outcome.gate_blockers` lists whichever of those conditions failed.

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
- **B2 and Hetzner.** Both use cellctl's own doubles, and the key custodian runs its real watch loop over a B2 double. Every per-cell key is the S3 double's one credential. D10's absence proof lists the live S3 double. The node-loss drill (`drill.py`) renders no custodian yet, so a new cell there has no backup key.
- **Neon and Vercel.** They are not involved: PostgreSQL is local and Substrate runs `next start`.

## Steps

1. **Invite.**
2. **Provision** a paid tenant (A): invite redemption, checkout, then a `subscription.activated` webhook. Timed until cellctl observes the cell running and ready. The target is under 3 minutes.
3. **OAuth MCP `tools/list`.** The stock MCP Python SDK OAuth client runs gateway 401, discovery, PKCE with `resource` (verifiers from the full RFC 7636 grammar), consent, a `127.0.0.1` loopback redirect and token. Warm `initialize` and `tools/list` are timed.
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

## Native Cloud import

The optional import journey runs the ordinary `exomem cloud import` command after provisioning.
It uses real Substrate HTTP, OAuth consent, PKCE, C5 rows, cellctl and import Jobs.
The CLI receives no prepared bearer token.
Versity serves versioned storage with TLS, and every actor trusts the run CA.
The CLI container shares Substrate's internal network and has no external route.

From the repository root, run the bounded journey:

```sh
uv run --frozen --project infra/cloud-rehearsal exomem-cloud-rehearsal run --steps 1,2 --cloud-import --report cloud-import-report.json
```

The report records source hashes, staged hashes, private receipt checks and cleanup evidence under `stages.cloud_import`.
The journey uses invented files and does not adopt them into governed notes.
The report remains partial and cannot gate the node.
The twelve P3 steps and their release criteria stay unchanged.
Production storage, browser CORS, large uploads and multiple nodes need their own acceptance evidence.

## Large agent-led import

`--large-import` proves `scale-agent-led-imports` task 5.1. It runs only on demand, as the `large-import` mode of the `Cloud rehearsal` workflow. It needs a runner with sudo, a loop device and the `dm_thin_pool` module.

A person's agent brings a large, realistic folder into a Cloud cell, then a second, media-heavy folder into the same cell. The cell runs on TopoLVM with a volume that cannot hold two copies of the first folder, so the run only fits when filing an artifact removes its staged copy.

For each folder the run does what an agent does:
1. Run `exomem cloud import <folder>` in the CLI container, as in the native import above.
2. Poll `observe_transfer` until the import is `done`.
3. Page with `import_memory`: `start` (then `start(after=...)`), `select`, `plan`, `apply`, `finish`. Every call keeps the default, compact reply, and the run reads its control fields from it. The compact reply omits the inventory, the recorded stage and the filed count, so the run also reads `read_import` status after `start` and after `finish`. If a compact reply drops a control field that the run holds, the `compact_reply` criterion fails.
4. Read the residual, then call `transfer_memory` with `import_discard` and `confirm=true`.
5. Wait for the index queues to empty, then read the Sources back.

The fixture generator (`large_import_fixture.py`) is deterministic. It writes its own manifest of path, size, SHA-256 and expected disposition. The pass criteria come only from that manifest:
- the stage's manifest proof is recorded, and the stage matches the files sent;
- every filed file has a Source whose SHA-256 matches;
- the residual equals the expected set, counted by reason;
- no write fails for lack of space;
- `consumed` equals the count of filed artifacts;
- the volume claim and the cell row stay at the fixed size (design D7: a local-storage cell publishes no limits and never grows).

The platform chart leaves `cloudGateway.transferTools` off, and so does the rehearsal (`platform.TRANSFER_TOOLS_ENABLED`). While it is off, the cell refuses `observe_transfer` and `transfer_memory` with `CLOUD_GATEWAY_REQUIRED`. The run reports that refusal and makes the discard through the Substrate route instead. With the setting on, the same refusal fails the run. The step summary prints `observe_transfer.served` and `discard_call.route` for each folder, so a pass shows when the transfer tools were not exercised.

The first folder holds about 80 percent of the bytes, and the file count is fixed at 70,000 by default. The byte total comes from the runner's free disk. The generator shrinks bytes before it shrinks the file count, and the report records the size chosen and why. The volume is 1.6 times the first folder, so two copies of it cannot fit.

The exit code is 0 for `pass` (or `smoke_only` with `--large-import-smoke`), 1 for a recorded product finding or a failed criterion, and 2 for a harness failure. `--large-import-minutes` (default 115) is a soft deadline that is checked between calls. When it passes, the run stops and reports what it finished as a failure.

Each page in the report keeps the counts and seconds of every `apply` call (`apply_steps`) and the files filed per second. The sampler also records the lexical repair result and duration from `/health/ready`. The run saves the last 20 MiB of each file in the cell's app log folder to `cell-app-<label>-<file>` in the diagnostics artifact, next to `cell-pod.log`. It saves them before each pod replacement the run causes: before each folder's upload (label `<folder>-before-upload`) and before its discard (label `notes` or `media`). It saves them again at the end of the stage or on failure (label `end`). A replaced pod takes its log folder with it, so together the copies cover every pod that serves the run's tool calls. The short-lived pod between the import's prepare and import Jobs is not kept. A label names when a copy was taken: `media-before-upload` holds the pod that drained the notes folder after its discard.

The report records, under `stages.large_import.measurements`: peak volume use, the cell's memory during the index build, `read_import_index` time per call, wall time per phase, and apply MiB/s for the large binaries. Each states its window and source. The volume and memory peaks are lower bounds from a 10-second sampler, and the sampler cannot read the cell while an import or discard Job holds its volume.

Runners differ in CPU speed by 2 to 2.4 times, so a timing from one run does not compare with a timing from another. The report's `environment` records `cpu_model`, `cpu_count_lscpu` and `cpu_mhz` from `lscpu` (`null` where `lscpu` does not give one). It also records the best of three passes of a fixed single-threaded workload (`host_speed.py`), which runs for a few seconds before any build: `host_calibration_zlib_seconds` for zlib compression, `host_calibration_sha256_seconds` for SHA-256, every pass, and the workload's definition (`host_calibration_workload`). SHA-256 runs about 6 times faster on a CPU with SHA-NI, so only the zlib seconds scale the timings, and the SHA-256 seconds tell an instruction-set difference from a general-speed one. Under `measurements`, `phase_seconds` and the page 1 `apply_seconds` of each folder stay raw, and `phase_seconds_per_calibration` and `apply_seconds_per_calibration` divide them by the calibration ratio (the zlib seconds over the reference), with the ratio in `calibration_scale`. The scaled values assume CPU-bound work, and the cell shares the runner's CPU with K3s, Postgres and the harness, so the raw values are the record. Without a calibration, the scaled values are `null`.

To run it from a workstation with Docker, sudo and enough free disk, use the `--large-import` flag of `exomem-cloud-rehearsal run`. Dispatch it on GitHub with `gh workflow run cloud-rehearsal.yml -f mode=large-import`.

## Raw-data import

`--raw-import` proves that messy input survives a Cloud import as well as a local one, and runs only on demand as the `raw-import` mode of the `Cloud rehearsal` workflow.
The run generates a collection and its answer key with `benchmarks/collection_ingest/generate.py` (`--profile standard --seed 7`), so new raw-data cases in the generator need no change here.
It sends the collection through `exomem cloud import` and the cell's MCP tools to a new vault, and to a second vault that first adopted another collection (`--profile small --seed 8`).
It copies each vault out with one streamed read-only `tar` of the pod's volume, which costs less than the owner export, and scores the copy with `score.py`.
It runs the same journey locally with `drive.py --profile personal`, then compares each key item between the two arms: a loss is an item that local kept and Cloud did not.
The exit code is 0 only on a pass, which means no loss on the first pass, no unknown read or measurement gap, and no changed, missing or copied pre-existing source page; run it with `--raw-import-python <interpreter with fastmcp and the server installed>`.

## Owner export

The optional owner-export stage connects the real Substrate owner endpoints to C6,
the running controller, the export Job and versioned storage. It creates one
invented source in the disposable owner's cell, requests its export, downloads
the issued version and runs the canonical archive verifier in the Cloud image.
It then expires only that fixture's row and checks deletion and return to Ready.

Select `owner-export` in the existing `Cloud rehearsal` workflow. This runs steps
1 and 2 before the C6 stage. It does not qualify a production node or enable a
production export flag. The default twelve-step rehearsal stays unchanged.

The stage enables `EXOMEM_CLOUD_EXPORTS_ENABLED` only in its isolated app.
It uses the existing sealed network, synthetic sessions, migrations and grants.
The report retains source and image identities, archive digests, states and
cleanup observations. It records failures without provider URLs or credentials.

The pinned Versity store supplies TLS, multipart requests, versions and cleanup.
It does not implement provider encryption at rest. Use the separate B2 task 4.2
receipt for that proof; this stage makes no encryption or per-cell key claim.

## Interrupted operator restore

`--operator-restore-abort` adds one stage after provisioning. It also works with only steps 1 and 2 selected.
The twelve default steps stay unchanged, and selecting this option always keeps `gates_node` false.
Dispatch `Cloud rehearsal` with mode `operator-restore-abort` for the bounded CI proof.

From the repository root, run the same stage:

```sh
uv run --frozen --project infra/cloud-rehearsal exomem-cloud-rehearsal run --steps 1,2 --operator-restore-abort --harness-check --report operator-restore-abort-report.json
```

The stage takes snapshot S0 through the controller, then makes a governed write and a stable host sentinel.
It runs the shipped `cloud_restore.sh run restore` entry in one disposable operator container.
That container uses real K3s and the shipped break-glass binding, with only the disposable cluster's admin configuration.
It mounts the exact disposable PVC directory read-only for one kernel event observer.
The observer watches both partial directory inodes before TERM, with no recursive watches or recorded filenames.
It records writer closure, partial file and root removal, and original root return in order.
Overflow makes the proof incomplete.
The configuration retains certificate verification and the cluster CA while using the K3s bridge address.
The object store uses HTTPS, and the cell image trusts the run CA.

A test layer preserves real restic for every command except this run's interrupted in-place restore.
After both original roots move, that restore creates distinct partial roots and stays an active writer until deletion.
The stage proves this state before sending TERM to the actual Bash child process.
The shipped recovery must stop the writer, restore both roots, resume the cell and resume controller reconciliation.
The interrupted operator run must exit nonzero even when recovery succeeds.

The report records helper source hashes, writer evidence, post-S0 data, ownership, readiness and resource absence before global teardown.
It checks recall of the post-S0 write and a new governed write with recall after recovery.
It also checks the stable host sentinel and owner-only modes, without comparing changing databases or caches.
The observer flushes its final state to stdout before exit; Docker retains that record after discarding the private tmpfs.
The diagnostics artifact includes `operator-restore-abort.log` and `operator-restore-abort-witness.json`.
A requested stage failure exits 2, including with `--harness-check`.

Ordinary governed writes do not initialize signed standalone custody, so signed custody recovery remains unproved.
This stage does not qualify production storage, the systemd launcher, natural restic failures or other signal paths.

## Report

`schema: exomem-cloud-rehearsal-report-v1`. It records:
- the pinned inputs and image digests;
- every host adaptation and every rehearsal overlay on the chart (storage class, cellctl doubles);
- each step's status, seconds, evidence and failure;
- the 5.3 measurements against their targets;
- the cross-lane defects found, with their owners.

It never contains a secret.

Logs are snapshotted after every step, and again before each in-step pod replacement: the upgrade, the canary, both read-only transitions and the backup window. The content-free check therefore covers pods that are later replaced or deleted.

The run is marked not valid, and cannot gate the node, when the cell image was supplied with `--cell-image` or when only some steps ran (`--steps`).

## Host adaptations

K3s-in-Docker adaptations are detected, applied only where needed, and each one is listed in the report:
- cgroup v1 hosts;
- hosts that withhold CAP_SYS_RESOURCE;
- image GC on a shared disk;
- system images imported instead of pulled.

`--gateway-build host` is for machines whose `docker build` cannot reach the npm registry with a trusted CA. It assembles the gateway image's final stage from a host build, and the report records that it did.

## Local-storage node-loss drill

`move-cloud-cells-to-local-storage` task 4, with the rehearsal evidence for
tasks 2.3, 2.4, 2.9, 2.10, and the planned move of 6.4 and 7.3. It needs privileged Docker, loop devices and `sudo`
for `losetup` and `modprobe`, so it runs on a GitHub runner: dispatch the
`Cloud rehearsal` workflow with `mode` set to `local-storage-drill`.

```
cd infra/cloud-rehearsal
uv run --frozen exomem-cloud-rehearsal local-storage-drill --report local-storage-drill-report.json
```

It runs a K3s server with embedded etcd and two agents, each with its own
loop-device `cells` volume group, and a third agent for the planned move.
TopoLVM, the snapshot controller, the cell
classes and cellctl come from one render of the platform chart with
`cellStorage.local.enabled`. The control database holds cellctl's fixture
schema, and cells are seeded as rows.

The checks run in order, each on the state the one before leaves:

1. **2.3:** three hourly snapshot backups of a serving cell on agent A.
2. **4.1 and 2.4:** one more write, then agent A and its disk are destroyed.
   The cell is relocated onto agent B from its last backup and accepted.
3. **4.2:** the backed-up cell started on an empty claim refuses to start.
4. **4.3:** a relocation's restore is interrupted. The cell stays down, the
   retained volumes do not change, and the retried restore serves.
5. **2.9:** the server is restored from an etcd snapshot older than a second
   cell, which is re-adopted by the runbook's steps.
6. **2.10:** a filler file beside the first cell's vault takes it past 80% use.
   Its next backup grows it online from 4 to 8 GiB, the drill's cap. Filled
   again, it stays at 8 GiB and cellctl raises `storage-growth-blocked`.
7. **6.4 and 7.3:** agent B is cordoned, a note is written to the first cell,
   and the cell is marked for a planned move. cellctl takes a stopped backup
   on agent B, retains the old volume and restores onto agent C. The cell
   serves there with the note, its row records the new volume, and it is
   accepted. The restore waits while only the cordoned agent B has a pool.

The report (`exomem-local-storage-drill-report-v1`) records each check's
evidence, the measured recovery point and recovery time, and the backup upload
times, each with its source. Exit 0 means every check passed, 1 a check
failed, and 2 the drill itself failed.

The agents share the runner's kernel, where device-mapper names are global,
so two `cells/pool0` pools cannot be active at once. Agent B therefore gets
its volume group only after agent A's disk is destroyed, as replacement
capacity. In check 7, agent C gets its volume group only after the move's
backup succeeded and agent B's pool was deactivated, with the second cell
stopped first. The report lists this and every other adaptation.

The drill does not move a cell between classes, as the migration from Hetzner
volumes (task 7.3) does: it has no Hetzner CSI driver, and its cellctl runs
with the local class as the domain. P3's coexistence configuration (task 7.7)
has the Hetzner class, on K3s's local-path provisioner, but no agent with a
local pool. That configuration with a storage-labelled agent and a move check
for a seeded Hetzner-class cell would rehearse the move between classes.

## Private-link rehearsal (k3s-link)

`move-cloud-cells-to-local-storage` tasks 6.5 (flannel MTU), 5.7 (agent-token
rotation) and 5.8 (client-CA rotation). It runs a K3s server with embedded etcd
and one agent, with no cell images, in about ten minutes. It needs privileged
Docker and `uvx`, but no `sudo`, so it also runs on a workstation. On a runner,
dispatch the `Cloud k3s-link rehearsal` workflow. A pull request runs it when
the k3s role's defaults, its flannel template, re-save helper or client-CA
helper, the K3s image pin, the ansible-core pin or this harness changes.

```
cd infra/cloud-rehearsal
uv run --frozen exomem-cloud-rehearsal k3s-link --report k3s-link-report.json
```

`--label` and `--image-label` add a `key=value` label to every container,
network and volume it creates, and to the image it builds. That image is
Alpine with `iputils-ping` and `openssl`. It runs the ping pods, and the k3s
role's client-CA helper, which the K3s image has no `openssl` for. The helper
runs in the server's network namespace, on the server's data volume and with
the server's own `k3s` binary.

The pinned ansible-core renders the k3s role's flannel configuration and the
values the checks expect from the role's own template and defaults. It uses the
role's `k3s_flannel_mtu` once it is set. While it is unset, before the rollout
commit, the rehearsal uses the smallest dedicated link the role declares
(`k3s_vswitch_mtu`, `k3s_wireguard_mtu`), the value that commit sets. The run
stops before any node starts if the K3s image does not report the role's
`k3s_version`. The Docker network runs at the Hetzner Cloud network's 1450
bytes, and the agent lowers its interface to a vSwitch's 1400.

It does not exercise the role's handler that recreates `flannel.1`. Both nodes
start with the flannel configuration present, and a container restart also
recreates the node's network namespace, so no old device survives a restart.

The checks run in order. A failed 6.5 check blocks only the later 6.5 checks.
A failed 5.7 or 5.8 check blocks the later 5.8 checks. The client-CA rotation
runs on the agent token that 5.7 rotates, as the runbook requires:

1. **6.5:** a ping with fragmentation forbidden crosses the agent's link at
   1400 bytes and fails at 1450, in both directions.
2. **6.5:** `flannel.1` and `FLANNEL_MTU` on both nodes equal the role's pod
   MTU, the values the role's handler asserts.
3. **6.5:** a pod on each node has `eth0` at the pod MTU, and a ping of that full
   size with fragmentation forbidden crosses the link both ways.
4. **5.7:** the server's agent token changes from A to B. The server restarts,
   then restarts again without the re-save. The check records whether the
   second restart stops on K3s's `newer than datastore` fatal. If it does, the
   check removes `server/cred/passwd` and starts the server, as the message
   directs.
5. **5.7:** the role's re-save helper leaves the server token file unchanged,
   and the server then restarts ready.
6. **5.7:** the supervisor answers `/v1-k3s/readyz` with 401 for token A and
   200 for token B, each presented alone.
7. **5.7:** the agent moves to token B, loses its kubelet client certificate,
   and restarts. It must get a new certificate and run its pod again. With
   its certificate it would rejoin on that alone.
8. **5.8:** the check copies the agent's kubelet client certificate, key and
   node password, which a lost disk holds. With them, three requests get 200.
   The API server returns the agent's own Node. The supervisor issues a
   kube-proxy certificate, and a kubelet certificate, which also needs the
   node password.
9. **5.8:** the role's client-CA helper loads a new client CA. The server
   restarts, then the agent as soon as the server is ready. The agent first
   loses its client keys, as the runbook directs for the host whose disk left
   custody. Both nodes must be Ready, the server must serve the CA the helper
   made, and the agent must hold it. The check records each restart's time to
   Ready.
10. **5.8:** the three requests of check 8 get 401 with the copied
    certificate. With the certificate the agent got after the rotation, on a
    new key, they get 200. Check 6 shows that the disk's agent token gets 401.
11. **5.8:** a new pod that the scheduler places on the agent runs, and
    `kubectl exec` reaches it through the agent's kubelet.

The report (`exomem-k3s-link-rehearsal-report-v1`) records each check's
evidence and the rendered configuration. Exit 0 means every check passed, 1
a check failed, and 2 the rehearsal itself failed.
