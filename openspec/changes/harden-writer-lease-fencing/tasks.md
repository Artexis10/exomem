## 1. Design + spec

- [x] 1.1 Confirm the coordinator's fencing-token contract (`src/exomem/lease_coordinator.py`) and whether a check-and-fence call exists or the local staleness flag is authoritative.
- [x] 1.2 Finalize `specs/writer-lease-fencing/spec.md` (expand scenarios: renewal-rejected mid-write, coordinator-reports-newer-holder, lease-disabled no-op, read-only bypass). `openspec validate harden-writer-lease-fencing --strict`.

## 2. Implement

- [x] 2.1 Thread the fencing token from `ensure_writer()` through `writer_lease.invoke` into the write path.
- [x] 2.2 Re-validate the token at the `vault.batch_atomic_write` commit boundary; raise `WRITER_FENCED` + clean up staged temps on a stale token.
- [x] 2.3 Ensure the renewer marks the token stale synchronously enough for an in-flight command to observe.

## 3. Verify

- [x] 3.1 New test: simulate lease loss mid-write (short TTL, replica A paused past expiry, replica B acquires the next token, resume A) → A's write is refused `WRITER_FENCED`, no bytes land.
- [x] 3.2 Existing `tests/test_writer_lease.py` + coordinator tests stay green.
- [x] 3.3 Latency gate green (`tests/test_latency_gate.py`) — the fencing check adds no per-write blow-up.
- [x] 3.4 `uvx ruff check` scoped to changed files only.
