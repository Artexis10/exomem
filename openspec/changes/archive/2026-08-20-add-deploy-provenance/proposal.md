## Why

Nothing in the running system reports **where its code came from**. `/health` returns a
version string and nothing else, so answering "what is deployed, and from which venv" requires
knowing to run `nssm get exomem Application` — undiscoverable unless you already suspect the
answer.

On 2026-07-20 that cost most of a deploy session. The NSSM service runs
`...\exomem-service-ha\.venv\Scripts\python.exe` while its `AppDirectory` is the primary
checkout, so the primary *looks* like the deploy target and reports a plausible-but-wrong
story: `uv sync` there changes nothing the service runs, and `/health` keeps reporting the old
version through any number of clean restarts.

The same session exposed a second, silent failure. Because that service venv is PyPI-backed
rather than repo-backed, `uv pip install --upgrade "exomem[embeddings,media]"` has no
visibility into `[tool.uv.sources]`, where the `pytorch-cu132` pin lives. The upgrade resolved
`torch 2.12.0+cu132` → `torch 2.13.0` (default PyPI, CPU-only on Windows), silently dropping
GPU capability and breaking the desktop/laptop same-wheel invariant the pin exists to protect.
`doctor` reported it, but only as one advisory line among many PASS rows.

Both failures share a root cause: **deploy provenance is invisible, and the one signal that
would catch a bad deploy is not a gate.**

## What Changes

- Add a `deploy-provenance` capability that reports how the running server was installed:
  version, install source (editable checkout vs installed wheel), git revision when
  repo-backed, the resolved torch wheel build tag, and which optional extras are present.
- Extend the public `/health` route with the non-sensitive subset of that provenance.
  `/health` is unauthenticated and publicly reachable through the tunnel, so it MUST NOT
  expose absolute filesystem paths, usernames, or host layout.
- Add an `exomem install-info` CLI command that reports the full detail, including the
  interpreter path, for local operators who need to identify the actual deploy target.
- Add a `scripts/deploy.ps1` that resolves the service's real interpreter from NSSM rather
  than assuming the checkout, upgrades it while preserving the CUDA index pin, gates on
  `doctor` including CUDA capability, restarts, and verifies the deployed version matches the
  requested one.
- Make the CUDA-capability check a **hard deploy gate** rather than advisory output, with an
  explicit opt-out for hosts that are legitimately CPU-only.

## Capabilities

### New Capabilities

- `deploy-provenance`: how the running server reports its own installation origin across the
  HTTP surface, the CLI, and the deploy script.

## Impact

- `src/exomem/server_assets.py` — `/health` payload gains provenance fields.
- `src/exomem/deploy_provenance.py` — new module computing provenance.
- `src/exomem/commands.py` — new `provenance` command on the shared surface.
- `scripts/deploy.ps1` — new deploy entrypoint.
- `docs/deployment.md`, `docs/release.md` — document the deploy path and the gate.

Backwards compatible: `/health` only gains keys. Existing `status`, `service`, and `version`
fields keep their current meaning, so tunnel and orchestrator probes are unaffected.
