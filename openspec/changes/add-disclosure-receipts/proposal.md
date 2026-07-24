# Proposal: add-disclosure-receipts

## Why

Governed releases and policy changes currently leave no durable, verifiable
record. For the professional use cases (legal, medical, consulting) and for the
owner's own trust in the system, a governed release should produce a
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
  the previous by hash; the chain head + monotonic seq are anchored in the
  per-machine `.governance.sqlite` so truncation is detected on load.
- **Proportional emission**: events fire only for governed decisions (any non-L6
  participation), policy changes, grants/revocations, deletions of governed
  material, and budget warnings. An ungoverned vault writes nothing. An optional
  per-scope "log everything" mode exists.
- **No plaintext**: a record carries refs, source and released **content hashes**,
  byte sizes, representation level, redaction counts, principal, audience, declared
  purpose, policy fingerprint, and confirmation type — never released content, and
  scope **ids + hashed labels**, not human label text. Full-text logging is an
  explicit opt-in.
- **Deletion events**: `delete_file`/`recover_from_trash` emit a
  deletion/inverse event so evidence survives source deletion without retaining
  the protected plaintext.
- **Chain verification** as a new `governance_receipts` audit category reachable
  via `maintain_memory(mode="audit")`.

The vocabulary is honestly "tamper-evident," never "immutable": on a personal
instance the owner can destroy both chain and anchor. Tamper-evidence targets
accidents, non-owner tampering, and org contexts where Hosted checkpoints
countersign — and the docs say exactly that.

## Capabilities

### New Capabilities

- `disclosure-evidence`: a proportional, per-machine, hash-chained,
  plaintext-free receipt log for governed releases, policy changes, grants, and
  deletions, with sidecar-anchored truncation detection and an audit-mode chain
  verifier.

## Impact

- Code: new `src/exomem/governance/receipts.py` (chained JSONL append, head cache,
  `verify_chain`); emission wiring in `governance/tool.py` (policy/grant/revoke —
  fsync'd), `governance/egress.py` (release/withhold — buffered), `governance/tokens.py`
  (mint/redeem); `src/exomem/delete_file.py` + `recover_from_trash.py` (deletion
  events); `src/exomem/audit.py` (`governance_receipts` category in
  `ALL_CATEGORIES`).
- Tests: `tests/test_governance_receipts.py`; `tests/test_audit.py` extension;
  deletion-event tests.
- Explicitly NOT in scope: signed checkpoints (SPECULATIVE, later), Hosted org
  checkpoints (Hosted wave), exportable compliance reports (later), budgets.
