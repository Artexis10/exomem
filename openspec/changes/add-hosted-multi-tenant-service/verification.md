# Verification — hosted multi-tenant service

Verified on 2026-07-12 in the isolated Exomem worktree.

## Evidence

- Full lean suite: `uv run --frozen pytest -q` — **2172 passed, 19 skipped**. Skips are optional model/media dependencies and platform-only tests.
- Latency gate: `uv run --frozen pytest -q tests/test_latency_gate.py` — **2 passed**.
- Two-cell lifecycle drill: `uv run --frozen pytest -q tests/test_hosted_private_routes.py -k local_two_cell_alpha_lifecycle_drill_preserves_isolation` — **1 passed**. The drill provisions independent cells, captures distinct sentinels, exports/restores one without derived sidecars, seals it, and proves the other remains available.
- Lifecycle/watcher/lexical rereview: **69 focused tests passed** after closing restartable-watcher and live-process preserved-mtime FTS gaps.
- Changed-file Ruff checks: clean.
- Gateway contract fixture: 21 registry-derived commands, pinned digest `983c4447f77ef31c1109b565e0149e053d222d87adabb84d5b3bc3581d1dfee2`; gateway focused suite **7 passed**.
- Strict OpenSpec validation: `openspec validate add-hosted-multi-tenant-service --strict --no-interactive` — **valid**.

## Security review closures

- Read admission is held across snapshot coordination and command execution; deletion sealing refuses active reads.
- Quiesced restart constructs dormant granted workers; resume rolls back started workers if durable activation fails; the real file watcher is restartable.
- The hosted binding marker is unavailable through direct reads and hidden directory listings.
- Corpus, hot-find, resolver, BM25, and FTS freshness share metadata signatures that detect preserved-mtime replacements, including live-process witnessed-write history.
- Export release persists a checkpoint before unlink, verifies the exact digest artifact, fsyncs the directory, and converges after lost acknowledgements.
- Hosted logs install process-wide fail-closed redaction before configuration parsing.

## External boundary

The Exomem cell implementation is complete and provider-neutral. Production compute/storage provisioning, live retention policy, and paid catalog pricing remain companion control-plane launch gates rather than cell responsibilities.
