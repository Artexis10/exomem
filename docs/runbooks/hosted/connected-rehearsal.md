<!-- authority:non-specification -->

# Connected hosted launch rehearsal

Run ordinary Substrate admission against a disposable provisioner API and K3s
cluster before attempting a live owner launch. The test drives fresh dynamic
storage, governance migration, serving readiness, Traefik ingress and the MCP
gateway. It captures a synthetic note, recalls its citation, restarts the runtime
Pod and reads the same note again.

The default 0.77.0 runtime reaches serving and draft validation, but
the real note commit refuses with `GOVERNANCE_CATALOG_PUBLICATION_BLOCKED`.
The connected test remains failing at that checkpoint. Capture, recall and
restart acceptance are not complete. The omitted navigation-catalog predecessor
was reproduced and repaired in PR #1324; repeat the connected journey only after
a reviewed released image containing that repair is selectable.

## Prerequisites

Use the paired Substrate checkout containing
`scripts/hosted-cluster-rehearsal.ts`, with its Node dependencies installed.
Install the pinned tools from `infra/tool-versions.env`, a working Docker daemon,
and the runtime and provisioner test dependencies. The test resolves its exact
runtime target from the companion Substrate checkout's reviewed registry before
creating a cluster. It reads that image from the local Docker image store and pulls that
immutable digest into its own K3s containerd; it builds the provisioner image
from the current checkout. Registry and Helm chart access are required.

From the Exomem checkout:

```sh
.uvbin/uv sync --frozen
.uvbin/uv pip install --python .venv/bin/python -e infra/provisioner
docker pull ghcr.io/artexis10/exomem@sha256:73ab2439e653d490b800eb810c370e297da0efcad176557756b46b89b5c82172
export SUBSTRATE_REHEARSAL_REPO=/path/to/substrate-checkout
export HELM_BIN=/path/to/helm
RUN_K3S_CONNECTED_LAUNCH_TEST=1 \
RUN_K3S_FIRST_PROVISION_DRILL_TEST=1 \
RUN_K3S_GOVERNANCE_DRILL_TEST=1 \
PYTHONPATH=src:infra/provisioner/src:tests \
.venv/bin/python -m pytest -q -s tests/test_hosted_k3s_connected_launch.py
```

The explicit connected lane fails if its companion checkout or required tools
are missing. A default suite that skips this opt-in test is not cluster evidence.

For a repaired release, first deliver its verified candidate, gateway/agent
fixtures and runtime target into the companion's trust registry. Set
`EXOMEM_REHEARSAL_RELEASE` to that exact version and pull the image it names.
Inspect the selection without credentials, a database or cluster effects by
running `node --import tsx scripts/hosted-cluster-rehearsal.ts --describe-runtime-target`
from the Substrate checkout with the same release environment variable. Unknown
versions are refused; the environment cannot supply arbitrary image or digest
overrides. The Python harness passes the complete selected target back to
Substrate, which rechecks it before creating its disposable database schema.
The final connection handoff must carry the same target. Selection does not
replace signed release verification or authorize a live deployment.

## Evidence and boundaries

Pytest's temporary directory contains the phase trace and final
`connected-serving-memory` receipt. On failure, retain that directory for the
first failing checkpoint. The private `substrate/node.log` records the connected
control-plane stages. Its credential handoff is mode 0600 and is removed on
normal cleanup; do not publish the private directory or handoff.

The test creates and removes its own uniquely named PostgreSQL and K3s
containers. It does not consume a live invitation or modify the hosted service.
The provisioner operation store is SQLite; Hetzner volume effects and CSI metadata
are represented locally. OAuth admission and token persistence run through the
real stores using cached client metadata; the browser consent and public token
endpoint are not exercised. A loopback proxy supplies the canonical control
hostname before forwarding through real Traefik; Node's native fetch does not
preserve an explicit Host override on a loopback URL. This substitutes local
DNS/TLS routing while preserving the upstream request and response. Local
forwarding represents TLS termination, and gateway rate limiting is bypassed
for this functional rehearsal.

The pinned v4 hosted contract returns command leaf responses. Capture verifies
the validated draft identity against the committed creation receipt. Recall
reads the returned citation and checks its content; compact search hits need
not contain the full observation.

A pass establishes this connected disposable journey. Live cloud behavior,
browser consent, semantic retrieval quality, renewal windows, backup recovery,
and genuine Claude-host acceptance remain separate launch evidence.
