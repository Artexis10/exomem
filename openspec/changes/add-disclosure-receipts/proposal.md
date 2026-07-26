# Proposal: add-disclosure-receipts

## Why

Governed releases currently leave no durable, verifiable record. For the
professional use cases (legal, medical, consulting) and for the owner's own
trust in the system, a governed release should produce a
**tamper-evident** receipt: what representation of which item left, to which
audience, under which policy version — without storing the plaintext that was
released. Exomem has no hash-chained or tamper-evident log today (verified); the
closest is the human `log.md` plus best-effort JSONL telemetry that logs query
text in plaintext locally. This change introduces the first chained evidence
store, deliberately scoped and proportional so ordinary personal recall stays
silent.

## What Changes

- A hash-chained append-only event log under
  `Knowledge Base/_Governance/events/<instance-id>/YYYY-MM.jsonl` — **one chain
  per machine** so a synced vault never forks a single chain. Each record links to
  the previous by hash; the per-machine `.governance.sqlite` separates the last
  fsync'd durable head from the observed-tail cache so recovery detects
  truncation without forking after a crash.
- **Proportional emission**: events fire only for governed decisions (any non-L6
  participation, including an always-on credential block), withhold-token
  lifecycle events, and deletion or recovery of governed material. Ordinary
  ungoverned L6 recall writes nothing. Policy-lifecycle wiring is owned by the
  dependent `add-governance-tools` change; budgets remain deferred.
- **No plaintext**: versioned event-type schemas select from a union of refs,
  source/released content hashes, sizes, level/outcome summaries, principal,
  audience, purpose, policy fingerprint, confirmation type, and scope ids/keyed
  label digests. Inapplicable fields are absent rather than fabricated. No event
  carries released content, credentials, or human label text, and there is no
  full-text mode.
- **Critical-event protocol**: deletion, recovery, and token consumption append
  and fsync a plaintext-free intent before state changes, then append and fsync a
  terminal event. A deterministic event id makes crash reconciliation idempotent.
- **Deletion events**: both file and directory deletion, plus
  `recover_from_trash`, use content-free tombstones around the move and derived
  index/media cleanup. Evidence survives source deletion without retaining
  protected plaintext, and stale derivatives cannot disclose a half-finished
  deletion.
- **Read-only chain verification** as a new `governance_receipts` audit category
  reachable via `maintain_memory(mode="audit")`; a write-capable reconcile path,
  never audit, repairs a lagging sidecar anchor.

The vocabulary is honestly "tamper-evident," never "immutable": on a personal
instance the owner can destroy both chain and anchor. Tamper-evidence targets
accidents, non-owner tampering, and org contexts where Hosted checkpoints
countersign — and the docs say exactly that.

## Capabilities

### New Capabilities

- `disclosure-evidence`: a proportional, per-machine, hash-chained,
  plaintext-free receipt foundation for governed release decisions,
  withhold-token lifecycle events, and governed content deletion/recovery, with
  sidecar-anchored truncation detection, critical-event reconciliation, and a
  read-only audit verifier.

## Impact

- Code: new `src/exomem/governance/receipts.py` (chained JSONL append, critical
  intent/terminal events, durable/observed heads, `verify_chain`, reconcile);
  content-free
  outcome collection in `governance/egress.py` with explicit once-only emission
  at the existing search, direct-read, graph, overview, structure/review,
  download, frame, and prompt/resource boundaries; `governance/tokens.py`
  (mint/redeem); `delete_file.py`, `delete_directory.py`, and
  `recover_from_trash.py`; `audit.py` (`governance_receipts` category) and the
  existing write-capable reconcile route; policy-source discovery excludes the
  reserved events/tombstone operational namespaces.
- Tests: `tests/test_governance_receipts.py`; `tests/test_audit.py` extension;
  deletion-event tests.
- Explicitly NOT in scope: policy/grant authoring (the dependent governance-tools
  change), signed checkpoints (SPECULATIVE, later), Hosted org checkpoints
  (Hosted wave), exportable compliance reports, budgets, or plaintext logging.
