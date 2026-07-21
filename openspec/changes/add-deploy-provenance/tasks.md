## 1. Provenance module

- [x] 1.1 Add `src/exomem/deploy_provenance.py` with a `provenance(include_local: bool)` entry
      returning version, install source, revision, torch build, accelerated flag, and extras.
- [x] 1.2 Detect install source via the distribution's `direct_url.json` / `__editable__`
      markers; fall back to `unknown` rather than guessing.
- [x] 1.3 Read the torch build tag from `importlib.metadata` only — never import torch.
- [x] 1.4 Detect extras with `importlib.util.find_spec` so no extra module code executes.
- [x] 1.5 Resolve git revision only for editable installs, tolerating a missing git binary.

## 2. HTTP surface

- [x] 2.1 Extend `/health` in `src/exomem/server_assets.py` with the public provenance subset.
- [x] 2.2 Guarantee the route cannot raise: wrap provenance in the existing defensive try.
- [x] 2.3 Assert no absolute path or username reaches the public payload.

## 3. CLI surface

- [x] 3.1 Add an `install-info` command exposing full detail including interpreter path.
- [x] 3.2 Keep `install-info` CLI-only, matching `doctor`. Both are local operator
      diagnostics about the host install rather than vault operations, and `doctor` is
      deliberately absent from the MCP tool surface. Exposing install paths and host
      layout over MCP would also work against the `/health` path-withholding decision.

## 4. Deploy script

- [x] 4.1 Add `scripts/deploy.ps1` resolving the interpreter via `nssm get exomem Application`.
- [x] 4.2 Abort when the resolved interpreter is missing.
- [x] 4.3 Capture the torch build tag before and after the upgrade.
- [x] 4.4 Fail on accelerator regression unless `-AllowCpuTorch` is passed; print the
      `--index-url https://download.pytorch.org/whl/cu132` repair command.
- [x] 4.5 Run `doctor --profile hybrid` as a gate before restarting.
- [x] 4.6 Restart via the existing `restart.ps1` path rather than duplicating service logic.
- [x] 4.7 Poll `/health` until the reported version matches the requested version or timeout.
- [x] 4.8 Print a final summary: requested vs observed version, install source, torch build.

## 5. Tests

- [x] 5.1 Unit-test provenance against wheel, editable, and unknown install shapes.
- [x] 5.2 Unit-test torch tag parsing for `+cu132`, bare `2.13.0`, and absent torch.
- [x] 5.3 Test that the public payload contains no absolute path (regression guard for the
      unauthenticated-endpoint leak).
- [x] 5.4 Test `/health` still returns 200 when provenance resolution raises.

## 6. Docs

- [x] 6.1 Document the deploy path in `docs/deployment.md`, stating that the service
      interpreter is authoritative and the checkout is not.
- [x] 6.2 Note the accelerator gate and its opt-out in `docs/release.md`.
- [x] 6.3 Cross-reference the PyPI-backed-venv failure mode so the runbook and the KB agree.
