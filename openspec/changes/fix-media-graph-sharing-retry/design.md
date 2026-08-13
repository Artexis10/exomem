## Context

`batch_atomic_write` always builds a floor → caller writes → checkpoint graph epoch when `vault_root` is supplied. Media processing asks to defer fanout so it can commit one canonical sidecar under the mutation guard and run derived work afterward, but the batch still injects the shared checkpoint. A Windows reader holding `.graph-sync.json` can make that final replacement fail after the sidecar changed; rollback can fail too, producing ambiguous `BATCH_ROLLBACK_INCOMPLETE` state that status mislabels as a damaged artifact.

Many graph protocol and recovery callers deliberately use `post_commit_fanout=False` while still requiring the complete epoch. That existing flag cannot be repurposed.

## Goals / Non-Goals

**Goals:**

- Keep the shared graph checkpoint and rebuildable graph/index data out of deferred media transactions while retaining the floor as a safety barrier.
- Preserve every existing graph epoch transaction unless a media caller explicitly opts into deferred graph completion.
- Carry the exact checkpoint across the mutation guard, publish it before fanout, and make missing convergence durable before canonical change.
- Recover a crash gap through real drain/startup paths without re-extracting a completed transcript.
- Classify legacy ambiguous failures as reconciliation-required, unhealthy, and non-retryable.

**Non-Goals:**

- Retrying an atomic batch after any destination may have changed.
- Removing graph checkpointing or weakening ordinary rollback.
- Adding a new queue, global lock, model, dependency, or ledger migration.
- Automatically deleting retained batch state or rewriting live graph files.

## Decisions

### Add explicit deferred graph completion

`batch_atomic_write` gains a narrow internal option valid only with immediate fanout disabled. It computes one exact checkpoint, stages its generation floor before caller writes, omits the shared checkpoint, and returns the exact deferred checkpoint plus its admitted predecessor to the media boundary. The floor keeps graph status recovery-required or unavailable across a crash gap.

After the mutation guard, media re-enters the canonical coordinator before checkpoint publication. It verifies the floor still equals the deferred generation and the checkpoint still equals the admitted predecessor, then publishes the exact token. If another writer advanced the epoch, the stale media token is not published and fanout is not claimed complete; its write-ahead receipt remains for recovery. The success path must never regress a newer epoch, substitute an old checkpoint, or manufacture a full-scope replacement.

All callers that do not opt in retain the existing floor → caller writes → checkpoint behavior, including graph-internal callers with `post_commit_fanout=False`.

Alternatives rejected: retry the checkpoint replacement after earlier writes may have committed; omit both floor and checkpoint, which could leave the old graph falsely current; repurpose `post_commit_fanout`, which breaks graph protocol callers.

### Write recovery work ahead of canonical change

Before the floor/sidecar batch, media admits a revisioned full-refresh receipt. Admission failure aborts before floor or canonical mutation. After commit it publishes the exact checkpoint, runs graph/index fanout, and CAS-clears only the admitted revision after every required component either completes or installs its existing verified durable exact downstream handoff. Any failed, degraded, missing, or unverifiable handoff retains the receipt. This definition permits quiet-mode graph/semantic handoff without falsely clearing lost work or pinning receipts forever. Crash, checkpoint publication failure, fanout failure, or a concurrent receipt revision leaves durable work queued.

Full-receipt drain and watcher startup validation recognize floor-ahead recoverable state, publish/recover a checkpoint before graph work, then clear receipts only after success. A committed transcript is never re-extracted.

Alternative rejected: rely on the current exception handler to add the receipt. That persistence is best-effort and happens too late to close the crash gap.

### Treat every trusted batch-error ambiguity as governed reconciliation

The strict bounded classifier accepts a valid stored `BatchWriteError` envelope. `BATCH_ROLLBACK_INCOMPLETE` becomes reconciliation-required and non-retryable. A trusted `BatchWriteError:` prefix with malformed, truncated, invalid, oversized, or malicious payload receives the same classification without target authority; unrelated errors retain generic behavior. Both `jobs[].error` and top-level `errors[]` replace the raw stored text with one bounded stable diagnostic for ambiguous malformed input.

The ledger stores no separate expected binary identity, so canonical sidecar provenance is the authority. Targeted retry reconciles before requeue: a complete transcript resolves only when its provenance matches the current binary; a fresh guarded attempt is permitted only for a matching pending sidecar; missing/conflicting provenance or changed input remains blocked/stale. Retained batch workspaces remain inspect-only.

Status exposes a top-level reconciliation-required count, reports `healthy=false`, preserves validated bounded target facts, and names the targeted media retry action without calling the binary corrupt.

## Risks / Trade-offs

- [The floor can remain ahead after a crash] → Write-ahead receipt plus real drain/startup recovery publishes a checkpoint before rebuild and retains fail-closed graph availability.
- [The new mode could be used without an owner] → Reject it with immediate fanout enabled, keep it internal, and test every opt-in call site.
- [A newer mutation arrives during convergence] → Revision-CAS clearing preserves the newer receipt.
- [Legacy text is truncated or spoofed] → Trusted malformed prefixes fail closed without target authority; unrelated text never gains system authority.
- [Native sharing differs from mocks] → Keep deterministic cross-platform tests plus a real Windows handle-sharing test.

## Migration Plan

No schema migration is required. Legacy failed rows are classified on read. After deployment, use targeted media retry: it reconciles the current sidecar and binary before any new attempt. Rollback is a code revert; canonical files, ledger rows, graph state, and deferred receipts remain compatible.

## Open Questions

None.
