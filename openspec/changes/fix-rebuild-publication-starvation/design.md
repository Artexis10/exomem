## Context

Managed lexical recovery currently builds a replacement SQLite catalogue beside
the live sidecar and publishes it only when `_live_publication_guard()` is byte
for byte unchanged. Production evidence showed the live catalogue WAL and the
detached rebuild WAL growing concurrently for more than ten minutes. Ordinary
writer and watcher work therefore changes the disposable live SQLite set while
the detached worker is scanning, causing an otherwise current replacement to be
discarded. A later refused read schedules the next whole-vault pass, so normal
traffic can starve readiness indefinitely.

The Markdown vault, policy projection, semantic identity, and recall checkpoints
are authoritative. SQLite main/WAL/SHM sizes and mtimes are useful observations,
but they are not themselves source-of-truth state.

## Goals / Non-Goals

**Goals:**

- Make managed full repair converge while ordinary writes and watcher events
  continue.
- Retain a single detached owner for full-catalogue work while retrieval is
  unavailable.
- Publish only a catalogue proven current for the authoritative source,
  projection, policy, semantic identity, and complete retained delta.
- Expose privacy-safe progress and decline reasons.

**Non-Goals:**

- Change the lexical sidecar schema or make it canonical state.
- Block writers for the duration of a whole-vault rebuild.
- Weaken path, access-policy, checkpoint, or semantic-identity validation.
- Redesign pull-request CI; the heavyweight/nightly split remains separate.

## Decisions

### 1. Prove logical freshness, not disposable file stability

Publication will distinguish authoritative guard changes from SQLite byte churn.
A change to source/projection checkpoints, policy, or semantic identity remains a
hard conflict until reconciled. A main/WAL/SHM token change alone will not veto a
replacement whose logical proof still matches.

Alternative rejected: keep the exact SQLite token as a hard veto. That is the
observed livelock because normal maintained-index traffic mutates the token during
every long rebuild.

### 2. Rebase the completed replacement around a bounded publication barrier

The detached worker records the recall checkpoints represented by its completed
catalogue. It first replays the complete retained suffix off-barrier and proves
the resulting source snapshot. It then waits on a background-only publication
bound sized for a large foreground batch, compares its targets with the latest
live projection, and applies only a small final suffix while holding the barrier.
If a complete final suffix exceeds that cap, the worker preserves its completed
temp catalogue, releases the barrier, catches up without the cap, re-proves the
source, and retries. Retry count is bounded so sustained writes cannot turn one
repair flight into an infinite owner.

An incomplete delta, a suffix that remains oversized across the bounded retries,
failed validation, or a policy/semantic identity change preserves the live
catalogue and leaves repair pending for a later bounded flight. If no logical
generation changed, publication proceeds despite token-only churn.

Alternative rejected: hold the publication lock during the full scan. On the
observed vault this would block normal work for ten to fifteen minutes.

### 3. The detached worker is the sole managed full-rebuild owner

Managed startup, refused reads, and watcher maintenance enqueue repair through
the existing single-flight worker. While managed retrieval is unavailable, none
of those paths may invoke an in-place whole-catalogue rebuild. Repeated demand is
coalesced against the generation already being repaired; one worker does not
chain whole-vault passes in a single flight.

If a successfully published and promoted pass leaves an uncovered generation at
its bounded idle handoff, the next flight first re-proves the persisted catalogue.
A foreground delta may already have made that handoff current; in that case the
worker acknowledges it without another full scan. Failed or unpromoted passes do
not receive this shortcut, and a request arriving during the proof remains
level-triggered and forces the normal repair path.

Offline and deliberately unmanaged callers retain their synchronous correctness
path because no long-lived repair owner exists there.

### 4. Readiness follows the exact published proof

Only a successfully published catalogue whose persisted checkpoints match the
current live projection may promote retrieval. If the projection advances after
publication but before promotion, the work remains pending and the next bounded
delta catches up; readiness is never manufactured from a stale generation.

### 5. Report bounded, privacy-safe repair progress

Repair telemetry will report phase, attempt age/duration, and a stable abort
reason such as `source_changed`, `delta_unavailable`, `identity_changed`, or
`publish_conflict`. It will not include vault paths, note names, or contents.

## Risks / Trade-offs

- **An event can arrive after the final proof.** Readiness promotion compares the
  exact published checkpoints again; a later generation stays pending.
- **A retained delta can be incomplete.** Publication fails closed and preserves
  the current sidecar rather than guessing.
- **Replaying too much work can lengthen the publication barrier.** Final replay
  stays capped. A transient oversized suffix is caught up off-barrier and retried;
  repeated oversized suffixes exhaust the bounded retries, decline publication,
  and leave repair pending.
- **Another process can mutate the live sidecar.** Logical checkpoints and
  identity are re-read under the barrier; unexplained authoritative drift still
  aborts even though token-only churn does not.

## Migration Plan

No schema or canonical-data migration is required. Existing sidecars remain
readable. Rollback reinstalls the previous package; any replacement database or
temporary rebuild is disposable and can be regenerated from Markdown.

## Open Questions

None blocking implementation.
