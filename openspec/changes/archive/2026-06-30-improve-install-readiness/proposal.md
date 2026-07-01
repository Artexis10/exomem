## Why

exomem is already public-surface clean and already uses `uv` internally (`uv.lock`,
CI, deployment docs, sidecar setup), but the first-run experience still reads like a
personal Python project: the local quickstart leads with manual venv + `pip`, and a
new user has no single command that explains what is missing on their machine.

This makes the project harder to distribute than the code quality warrants. The next
OSS-readiness slice should make the install path deterministic and make failures
actionable without changing the server's runtime model.

## What Changes

- Make `uv` the canonical documented setup path for local development and first use,
  while keeping `pip install -e .` as a fallback for users who already manage Python
  environments themselves.
- Add a read-only `doctor` admin CLI command:
  `python -m kb_mcp doctor [--vault PATH] [--profile lean|hybrid|media|remote] [--json]`.
- `doctor` reports grouped setup checks with `PASS` / `WARN` / `FAIL`, concrete
  remediation text, and stable JSON for scripts.
- Keep the core install lean by default. Embeddings/media/diarization/vision remain
  optional extras and keep their existing soft-fail runtime behavior.

Out of scope: Docker images, package publishing, hosted control-plane work, and any
new model capability.

## Capabilities

### Added Capabilities
- `install-readiness`: uv-first setup documentation and a read-only local preflight
  command for lean, hybrid, media, and remote profiles.

## Impact

- Code: new diagnostic module plus a CLI-only admin subcommand in `__main__.py`.
- Docs: README and local setup become `uv`-first; pip becomes fallback.
- Tests: torch-free doctor unit/CLI tests with dependency checks stubbed where needed.
