## Why

The deferred-index queues are advertised as retryable work, but **nothing in the running
server ever retries them**. `index_sync.drain_deferred_work` has exactly one caller in the
package — inside `op_process_media`. The only other path that clears semantic entries is
`_index_main`, i.e. a human running `exomem index` by hand. No watcher pass, no reconcile
loop, and no scheduler drains either queue.

On 2026-08-13 this surfaced on the primary desktop vault as ~7 hours of "why are writes so
slow", and the diagnosis took an hour because every reported signal pointed the wrong way:

- `status --resources` reported `semantic_upserts: 2839` and `full_upserts: 2866` against a
  vault of 2,875 indexed pages — the entire corpus queued — with `retryable: true` and
  `next_action: "retry deferred index refresh"`. No command is named after that action and no
  code path performs it. The field reads as a promise the system makes to itself.
- `doctor` reported the vault healthy. A backlog the size of the whole corpus produced no
  warning at any severity.
- The backlog was **phantom**. A manual `exomem index` reported `files_to_embed: 0` across
  2,835 indexable files: every queued file was already embedded and current. The queue was
  stale bookkeeping accumulated by paths that satisfied the work elsewhere without clearing
  the entry.

The host had also been pinned in `quiet` mode since 2026-08-02, which made the condition
permanent. Quiet sets `defer_expensive_indexes: true` with `max_embed_files_per_batch: 0` and
`max_reconcile_embed_files: 0`. Deferral there is not a throttle, it is a halt: the reconcile
pass wakes every 900s, admits zero files, and sleeps. Combined with no drainer, the queue is
append-only by construction for as long as the mode is held.

The mode was held for eleven days because of a second, independent failure with the same
shape. `exomem mode performance` had been run on 2026-08-02 and had **silently not applied**.
On an NSSM service install the mode config is written by the service as LocalSystem, leaving
`%ProgramData%\exomem\config.json` with `BUILTIN\Users:(RX)`. The user-facing CLI command
therefore cannot replace it. The run died with a bare `PermissionError` traceback, left
`config.json.tmp` holding the intended `performance` value, and left `config.json` on
`quiet`. A documented user-facing command became a silent no-op whose only evidence was a
stack trace and an orphaned temp file.

A third upgrade defect shipped in v0.47.0: Windows idempotency runtime hardening protects
new directories but deliberately refuses to repair existing ones. An install upgraded from
v0.46.0 can therefore pass `doctor`, then stop the media worker on its first idempotency
store access with a pathless `unsafe Windows DACL` error. The failure neither identifies the
offending runtime path nor gives the operator a command that can make it safe.

Worst of the three: `full_upserts` cannot be cleared by anything. It is only released by
`clear_deferred_work(include_full=True)`, and `include_full` appears in exactly three places
in the package — its own parameter declaration and the two `if include_full:` branches it
guards. **No caller passes it.** That queue grows monotonically for the life of a vault.

## What Changes

- Give the running server a bounded, mode-aware drain pass for both deferred queues, so
  queued work is retried by the system that queued it rather than by a human who happens to
  know `exomem index` exists.
- Keep a background drain inside the mode's smaller live-publication batch even when the
  performance reconcile budget is larger, and bound per-receipt isolation after an
  incomplete batch so one pass cannot expand into hundreds of serial retries.
- Redefine quiet-mode deferral as a **throttle, not a halt**: a small non-zero admission per
  reconcile pass, on CPU, so a quiet host still converges instead of accumulating forever.
  Quiet trades throughput for latency; it must not trade away correctness.
- Reconcile queue entries against actual index state during the drain, so entries already
  satisfied by another path are retired rather than re-reported. A queue that reports 2,866
  files of pending work when zero files need embedding is a defect in its own right.
- Make `full_upserts` reachable: give the full-upsert queue a real drain/clear path, and
  reject the current state where a parameter exists that no caller can reach.
- Surface the backlog honestly. `status` MUST NOT advertise a `next_action` no code path
  performs; `doctor` MUST warn when either queue exceeds a meaningful fraction of the corpus.
- Make `exomem mode` non-silent: on a permission failure it MUST exit non-zero with one clear
  line naming the config path and the remediation, MUST NOT leave an orphaned `.tmp`, and
  MUST NOT report or imply success while the persisted mode is unchanged.
- Keep Windows idempotency state fail-closed while making legacy-DACL upgrade failures
  actionable: name the exact offending path and an exact `icacls` remediation command, make
  `doctor` surface the same fault, and document the principal-private runtime boundary.

## Capabilities

### Modified Capabilities

- `live-index-freshness`: deferred queues gain an owner — a bounded server-side drain, a
  throttled (not halted) quiet-mode policy, reconciliation of already-satisfied entries, and
  honest backlog reporting.
- `command-surface`: mode persistence failures become loud and actionable rather than a
  silent no-op with a traceback.

### Added Capabilities

- `windows-runtime-security`: pre-existing idempotency runtime state remains fail-closed, but
  an invalid DACL now produces a path-specific remediation and is visible in preflight.

## Impact

- `src/exomem/index_sync.py` — drain/clear paths for both queues; `include_full` reachable.
- `src/exomem/deferred_index.py` — reconciliation of already-satisfied entries.
- `src/exomem/file_watcher.py` — reconcile pass owns a bounded drain.
- `src/exomem/mode.py` — quiet policy caps become non-zero throttles.
- `src/exomem/__main__.py` — `_mode_main` failure path; `_index_main` clears both queues.
- `src/exomem/doctor.py` — backlog warning.
- `src/exomem/commands.py` — `status` next_action honesty.
- `src/exomem/mutation_lock.py` / `src/exomem/writer_lease.py` — actionable Windows runtime
  DACL failure without weakening the private-state validator.
- `CHANGELOG.md` / `docs/deployment.md` — Windows upgrade remediation and service/user state
  ownership boundary.

No behavior here runs a model that was not already running; the drain reuses the existing
embedding path under existing mode policy, so the pure-substrate constraint is unaffected.
The drain is bounded by mode policy and defaults to the same work the reconcile pass already
performs, so it is soft-fail and adds no steady-state GPU residency in `quiet` or `normal`.
