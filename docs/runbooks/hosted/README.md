<!-- authority:non-specification -->

# Hosted operations runbooks

Draft operator runbooks cover backend bootstrap, reviewed deploy, secret
handoff/rotation, cell lifecycle, maintenance, retained-volume rebind,
backup/restore, ordered deletion, node replacement, and break glass. A runbook
may orchestrate the versioned tools under `infra/scripts`; it may not contain a
credential, mutable image tag, tenant content, or destructive default.

They become release-authoritative only after every live private-alpha proof gate
in the active OpenSpec change is green. Until then, the owner canary is the only
deployment target and the runbook milestone remains deliberately open.

The machine-readable index is `infra/contracts/runbooks-v1.json`. Every runbook
has explicit preconditions and content-free verification; destructive paths name
their exact approval flag and remain fail-closed by default.
Implemented runbooks:

- [Secret handoff and rotation](secrets.md)

## Exact K3s hosted-runtime gate

Run the current-checkout gate before merging a hosted runtime change. It requires
Docker, Helm, and `uv` and proves the candidate code and chart together:

```bash
HELM_BIN="$(command -v helm)" \
RUN_K3S_RUNTIME_TEST=1 \
uv run --frozen pytest -q \
  tests/test_hosted_k3s_admission.py \
  -k current-private-modes
```

After the release is signed, advance
`infra/contracts/exomem-hosted-runtime-k3s-gate-v1.json` to that exact release and
rerun against its pinned source commit:

```bash
HELM_BIN="$(command -v helm)" \
RUN_K3S_RUNTIME_TEST=1 \
RUN_K3S_REVIEWED_RUNTIME_TEST=1 \
EXOMEM_RUNTIME_REPO="$(pwd)" \
uv run --frozen pytest -q \
  tests/test_hosted_k3s_admission.py \
  -k reviewed-private-modes
```

Both gates build the hosted target, load its computed digest into K3s
`v1.35.6+k3s1` pinned by OCI digest, and prove real PVC mounts, owner-only modes
and bytes across pod replacement, root-owned credential projection, private
authorization custody, non-root/read-only serving, and authenticated private
readiness. The reviewed lane additionally checks out the exact source commit
from the signed runtime-gate manifest. Each gate removes its temporary Docker
image and K3s container afterward.

The test PV is deliberately prebound and `hostPath`-backed inside the disposable
K3s node. Passing this gate does not replace the separate real Hetzner CSI/LUKS
attach and remount proof.
