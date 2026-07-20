## Context

The 2026-07-20 deploy session established the concrete facts this design responds to:

- Service interpreter: `...\exomem-service-ha\.venv\Scripts\python.exe` (standalone,
  PyPI-backed, non-editable `site-packages` install — **not** a git worktree).
- Service `AppDirectory`: the primary checkout, which is what makes the topology misleading.
- The cu132 pin lives in the repo's `pyproject.toml` under `[tool.uv.sources]` with
  `explicit = true`, so it is repo configuration and does not travel with the PyPI wheel.

## Goals / Non-Goals

**Goals**

- One request answers "what version is deployed, and from where".
- One command deploys a version and refuses to report success unless the running server
  actually serves it.
- A CPU-torch regression fails the deploy loudly instead of passing as an advisory line.

**Non-Goals**

- Deciding whether the service should return to a repo-backed venv. That is a real question
  (it would restore the lockfile pin path and make every existing runbook correct again), but
  it carries HA/failover implications and is tracked separately. This change makes the current
  topology *legible*; it does not change it.
- Multi-host orchestration or remote deploy. This targets the single-host NSSM service.

## Decisions

### Decision 1: `/health` gets provenance, but never filesystem paths

`/health` is unauthenticated by design — it is the tunnel/orchestrator liveness probe, and
its docstring commits to "no vault data, no auth required". It is reachable publicly at
`https://<host>/health`.

Absolute interpreter paths embed the OS username and host layout (`C:\Users\<name>\...`).
Publishing those on an unauthenticated endpoint is an information leak that serves no probe
consumer, so the public payload carries only:

```json
{
  "status": "ok", "service": "exomem", "version": "0.25.5",
  "install_source": "wheel",          // "wheel" | "editable" | "unknown"
  "revision": null,                    // git sha, only when editable
  "torch": "2.13.0",                   // wheel build tag, null when absent
  "accelerated": false,                // torch build carries a CUDA/ROCm local tag
  "extras": ["embeddings", "media"]
}
```

`install_source` and `revision` are the fields that would have ended the 2026-07-20 confusion:
a `wheel` install with a null revision immediately says "this is not running your checkout".

Full detail — including `interpreter` — is available locally via `exomem install-info`, which
runs on the operator's own host where the path is not a disclosure.

The command is named `install-info`, not `provenance`: this codebase already uses
"provenance" for note/source provenance (`src/exomem/provenance.py` scans `<!-- key:value -->`
tags in note bodies), and in a knowledge-base product that word would read as vault
provenance rather than install origin.

### Decision 2: Never import torch to determine the torch build

`/health` must stay fast and must never fail; importing torch costs seconds and allocates.
`importlib.metadata.version("torch")` reads the installed distribution's metadata without
importing the package, and on Windows returns the full local version tag — `2.12.0+cu132`
versus a bare `2.13.0`. That local tag is exactly the signal that distinguishes the CUDA wheel
from the default-PyPI CPU wheel, which is the regression we need to catch.

`accelerated` is therefore derived from the presence of a CUDA/ROCm local tag in the version
string, **not** from `torch.cuda.is_available()`. This is a deliberate difference in meaning:
`/health` reports *which wheel is installed* (a deploy property, stable and cheap), while
`doctor` continues to report *whether a GPU is actually usable right now* (a runtime property
requiring the import). Both are useful; conflating them is what let the regression hide.

### Decision 3: `deploy.ps1` resolves the target from NSSM, never from cwd

The script's first act is `nssm get exomem Application` to obtain the true interpreter. Every
subsequent step operates on that interpreter. This structurally prevents the failure where an
operator syncs the checkout they happen to be standing in and concludes the deploy worked.

The script refuses to run if the resolved interpreter does not exist, rather than falling back
to a guess.

### Decision 4: The CUDA gate is opt-out, not opt-in

After upgrading, the script compares the torch build tag before and after. If the host had an
accelerated build and now does not, the deploy **fails** and the script reports the exact
repair command. Hosts that are legitimately CPU-only pass `-AllowCpuTorch` to acknowledge it.

Defaulting to fail is the right asymmetry: a silent capability loss is expensive to discover
later (it took a full session), while a spurious failure costs one flag.

### Decision 5: Verify by polling `/health`, not by trusting the installer

`uv pip install` reporting success only proves the venv changed. The deploy is not complete
until the *running process* serves the target version, which requires the restart to have
taken effect. The script polls `/health` until `version` equals the requested version or a
timeout elapses, and fails otherwise. This is what turns "I ran the commands" into evidence.

## Risks / Trade-offs

- **`/health` payload growth.** Probes that assume an exact-shape body could be affected.
  Mitigated by only adding keys and preserving existing ones; JSON consumers ignore unknown
  fields.
- **`extras` detection cost.** Uses `importlib.util.find_spec`, which does not execute module
  code, so it stays cheap. A missing extra reports as absent rather than raising.
- **Version tag heuristic.** `accelerated` keys off local version tags (`+cu132`, `+rocm`).
  A future wheel could ship CUDA without a local tag, producing a false negative. Acceptable:
  `doctor` remains the authority on live GPU availability, and a false negative fails safe
  (blocks a deploy) rather than passing a regression through.
