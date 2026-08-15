## Why

Write-path advisories have no memory. The near-duplicate, contradiction-band, and overlap warnings that fire on `remember`, `capture_source`, and `edit_memory` consult no review state: a warning the user has already seen and declined re-fires verbatim on every subsequent write to the same page. The queue surfaces solved this problem long ago — fingerprint-bound dismiss/snooze with material-change resurfacing — but the write channel bypasses that machinery entirely. The predictable failure is habituation: an agent that has learned the warnings repeat starts ignoring them, which also mutes the true positives. The 2026-08-15 no-nudge architecture audit rated this the cheapest verified defect on the delivery path.

Separately, the write path and the audit disagree about what "relation debt" means. The write-result feedback clears the flag when `sources:` is present, while the audit category and the semantic-contract connectivity lane deliberately do not (provenance alone must not satisfy connectivity, or bulk imports would no-op the gate). An agent consuming both surfaces sees the same page reported clean at write time and flagged at review time.

## What Changes

- Give each write-path advisory (near-duplicate, contradiction-band, overlap) a stable review identity and a signal fingerprint with the same semantics as queue review items, namespaced apart from every existing queue.
- Consult portable review state before emitting: an advisory whose exact fingerprint was dismissed is not re-emitted; a material change to either endpoint produces a new fingerprint and the advisory returns.
- Honor the shipped competing-alternatives pair stance on emission: a pair the user has marked as deliberate rivals produces no duplicate advisory, composing with the existing stance contract rather than restating it.
- Unify the relation-debt predicate: the write-result feedback reports the same debt condition, under the same name, as the audit category and connectivity lane; provenance presence is reported separately instead of silently clearing debt.
- Keep every advisory failure-isolated and advisory-only, exactly as today: suppression state that cannot be read causes the advisory to be emitted, never the write to fail.

## Capabilities

### Modified Capabilities

- `command-surface`: write-path advisories carry stable review identities, are suppressed for exactly-dismissed fingerprints, resurface on material change, and report the unified relation-debt predicate.
- `attention-queue`: the portable review-state store records write-advisory decisions in their own identity namespace, with dismiss/snooze/reopen and material-change resurfacing semantics identical to queue items and no collisions with existing namespaces.

## Impact

The corpus-aware advisory emission path, the write-result feedback builder, the triage dispatch table (one new ref namespace), and focused tests. No MCP tool is added and no tool input schema changes, so the packaged tool-surface fingerprint is untouched. No new store: decisions land in the existing portable review-state file. Existing write-latency, governance, and mutation-safety gates continue to hold unchanged.

Deliberately out of scope: suppression or dismissal state for the structural-promotion suggestion (its shipped contract resolves by scope agreement, and per-family suppression belongs to the later nag-governance change); any change to detection thresholds; any queue-side behaviour change.
