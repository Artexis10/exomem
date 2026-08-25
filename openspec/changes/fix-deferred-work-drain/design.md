## Context

Two deferred queues live in `deferred_index.py`, backed by
`Knowledge Base/.deferred-index.sqlite`:

| Queue | Added by | Cleared by | Reachable? |
| --- | --- | --- | --- |
| semantic upserts | `index_sync.py:414` (`add`) | `clear_semantic_receipts`, `clear`, `drain_deferred_work` | only via `op_process_media` or a human `exomem index` |
| full upserts | `index_sync.py:263`, `:516` (`add_full`) | `clear_full`, gated behind `include_full=True` | **no caller passes `include_full=True`** |

`drain_deferred_work` (index_sync.py:594) has one caller in the package:
`commands.py:4689`, inside `op_process_media._drain_index_refresh`. `_index_main`
(`__main__.py:640`) calls `clear_deferred_work(vault_root)` with the default
`include_full=False`, which is why a manual `exomem index` empties the semantic queue and
leaves the full queue untouched — observed live on 2026-08-13: `semantic 2810 → 0`,
`full 2866 → 2868` (still climbing) across the same run.

The watcher already has the shape a drain needs. `file_watcher.py:857-880` computes a
`policy` per pass and distinguishes a reconcile pass (`cap`) from a live burst. It admits
files up to `max_reconcile_embed_files` / `max_embed_files_per_batch`, and in quiet mode
takes a third branch that only logs. What it never does is look at what is already queued.

## Goals / Non-Goals

**Goals**
- A queued entry is retried by the system that queued it, on a bound the mode chooses.
- Quiet mode converges. Slowly is fine; never is not.
- Reported backlog reflects real outstanding work.
- Both queues have a reachable clear path.
- A failed mode write is loud.

**Non-Goals**
- No new model, no new index, no change to what gets embedded or how. The drain reuses the
  existing embedding path under existing policy.
- No steady-state GPU residency added to `quiet` or `normal`.
- Not addressing warm-up readiness replay (`readiness.drain_deferred`) — that is a different
  queue, currently in flight on `fix/watcher-deferred-freshness`.

## Decisions

### Drain belongs on the reconcile pass, not a new scheduler

The reconcile pass already wakes on a mode-chosen interval (900s quiet, 300s performance),
already holds a policy object, and already bounds its own admission. Adding a separate
drain thread would duplicate that bounding and introduce a second writer contending for the
mutation boundary. The drain becomes a step of the pass, sharing its budget: the pass admits
drift first, then spends any remaining budget on queued entries.

**Alternative rejected** — drain on every write. It puts unbounded latency on the write path,
which is the symptom this change exists to remove.

### Quiet caps become non-zero

`_QUIET_EXPENSIVE_INDEX_CAP = 0` (mode.py:57) is the halt. A quiet host should still
converge; the question is only how fast. A cap in the 20–30 range per 900s pass clears a
2,800-file backlog in roughly a day and a half of idle time at a cost no one notices on CPU.

The exact number is a tuning decision, not a contract. The spec requires non-zero and
bounded; the implementation picks the value.

**Note**: quiet's third watcher branch (`elif policy.defer_expensive_indexes`) logs the
deferral but leaves `defer_semantic = False`, so `upsert_after_write` is called with
`defer_semantic=False` while the log claims deferral. Whether the deferral actually happens
downstream inside `index_sync` needs confirming during implementation — if the log is lying,
that is a second defect to fix here, and if it is not, the branch should set the flag it
implies.

### Reconcile queued entries against index state

The 2026-08-13 evidence is that queue entries outlive the work they describe: 2,810 entries,
zero files actually needing embedding. Rather than trusting the queue, the drain resolves
each entry against the same freshness check `exomem index` uses, and retires entries whose
work is already satisfied. This makes the queue self-healing against any future path that
satisfies work without clearing its entry — the class of bug that produced this state.

**Alternative rejected** — audit every `add`/`clear` call site for symmetry. Necessary but
not sufficient; it fixes today's leaks and not tomorrow's, and the queue would still report
a phantom backlog until someone noticed.

### `include_full` gets a caller or gets deleted

A parameter no caller can reach is not a feature. Either `_index_main` passes
`include_full=True` (making `exomem index` clear both queues, matching what an operator
already believes it does), or the full queue gets its own drain. Preference is the former
plus a drain, so the full queue is both self-draining and manually clearable.

### Mode failure is a first-class error path

`_mode_main` writes `config.json.tmp` then renames. On a service install the user cannot
replace the SYSTEM-owned target. The fix is not to elevate — it is to fail correctly: catch
`PermissionError` and `OSError` around the persist, remove the temp on failure, exit
non-zero, and print one line naming the config path plus the remediation. Reporting the
persisted mode after a write MUST read the file back rather than echo the requested value.

### Invalid Windows runtime DACLs fail with an exact repair command

The Windows private-runtime validator remains fail-closed. It MUST NOT silently rewrite a
pre-existing directory, because the process doing the repair may be a user CLI while the
runtime belongs to a LocalSystem service (or the reverse). Replacing the DACL under the
wrong principal would convert an upgrade failure into a lockout of the intended owner.

Instead, validation errors name the exact path and render an `icacls` command for the
current runtime principal plus the existing SYSTEM and Administrators recovery principals.
The command is documentation, not an invoked subprocess: Exomem does not elevate and does
not mutate an existing DACL implicitly. `doctor` preserves the structured failure rather
than converting an unreadable idempotency store into a healthy result.

The trustee contract is intentionally principal-private. LocalSystem resolves to `{SY, BA}`;
a normal user resolves to `{user SID, SY, BA}`. Because the validator requires an exact
trustee set, one runtime directory cannot satisfy both identities. Deployment documentation
therefore requires separate `EXOMEM_WRITER_LEASE_STATE_DIR` values for service-owned and
direct user-owned processes. Weakening the DACL to make a shared directory pass would also
conflict with the user-bound DPAPI receipt envelope and is outside this change.

That separation is not permission for concurrent mutation. The state directory also anchors
the host-local mutation coordinator, so two identity-specific roots would create two locks for
one vault. A direct user process MUST NOT mutate a service-owned vault while the LocalSystem
service is running; ordinary mutations route through the service, and direct maintenance runs
only while the service is stopped.

## Risks

- **Merge collision.** `fix/watcher-deferred-freshness` is unmerged and rewrites
  `deferred_index.py` (+247) and `index_sync.py` (+52). Sequence the two deliberately;
  do not let both land blind.
- **Drain contends for the write boundary.** Bounded admission plus the existing reconcile
  interval keeps it to the same envelope the pass already occupies, but the bound is the
  only thing preventing a quiet host from doing performance-mode work.
- **Retiring entries could retire real work** if the freshness check disagrees with what
  queued the entry. The check must be the one the indexer trusts, not a cheaper proxy.
- **A direct CLI and a service cannot share one private runtime directory.** This change
  documents the boundary and fails actionably; it does not redesign cross-principal local
  mutation coordination or weaken the idempotency secret boundary.
- **Separate private roots also separate host-local mutation locks.** Operators must quiesce
  the service before direct CLI maintenance; supporting concurrent cross-principal local
  mutation would require a separate lock-root design outside this change.

## Production follow-up: bound background publication, not only admission

The first production backlog after deployment exposed two bounds that were individually
finite but operationally unsafe together. Performance mode admitted 500 deferred files per
reconcile pass, split them into a 250-file full-index batch, and an incomplete component then
fell back to serial replay of every receipt. Against a large lexical catalog, one watcher
pass could therefore keep derived retrieval warming for tens of minutes even though writes
and the public transport stayed alive.

Background repair now takes the smaller of the remaining reconcile budget and the live
batch cap. This keeps performance mode's real-drift policy separate from the amount of old
queued work it may publish at once. When a bounded batch is incomplete, only a small fixed
prefix is isolated in that pass; attempted failures rotate behind untouched receipts and the
next periodic pass continues fairly. An unbounded operator drain keeps the exhaustive
isolation behavior because waiting for repair is the explicit purpose of that command.

The live cap can be overridden to zero or one. Zero still reserves one background slot when
reconcile budget remains: it may defer a live burst, but cannot turn durable repair back into
a halt. With one shared slot and both queues populated, a durable turn marker alternates the
slot between full and semantic work. Keeping the turn in the deferred-work sidecar preserves
fairness across restarts and avoids permanently favoring whichever queue is checked first.
The claim starts an immediate SQLite transaction before reading the turn, so concurrent
drains serialize the read-and-flip rather than both observing the same queue. Startup uses
the same unscoped allocator as periodic repair; targeting only full receipts there would
bypass the turn marker and let repeated restarts starve semantic work.

Alternatives rejected:

- Reducing only the performance reconcile cap conflates real missed-event admission with
  deferred backlog replay and leaves failure isolation unbounded.
- Removing isolation entirely lets one poison receipt pin every later item forever.
- Clearing a full receipt after only some component outcomes succeed loses durable repair
  authority for the failed components; per-component receipt custody would be a larger
  schema change.
