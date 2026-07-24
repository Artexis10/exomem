# Design: add-disclosure-receipts

## Context

No hash-chained log exists today (verified grep); append-only is filesystem/
policy-based (`access.py` tiers), and `log.md` is a human record. Precedents to
reuse: `mutation_terminal.committed_terminal` (`receipt_id`/`request_id` shape),
`privacy_log.install_hosted_log_redaction` (content never echoed),
`hosted_portability.classify_artifact` (what may leave a boundary),
`query_log._append` (best-effort local JSONL semantics). The chain lives under
`_Governance/events/` (in-vault, travels with the vault) but is partitioned by a
per-machine instance id, with the head anchored in the never-synced sidecar.

## Goals / Non-Goals

Goals: a proportional, plaintext-free, tamper-evident receipt for governed
egress and policy change; deletion evidence that outlives the source without its
plaintext; cheap chain verification in the existing audit surface; honest
vocabulary.

Non-Goals: signed checkpoints, Hosted org countersignatures, exportable
compliance reports, budgets (all later). No plaintext in the log. No receipts for
ungoverned recall.

## Decisions

### D1 — One chain per machine, sidecar-anchored
Records live in `_Governance/events/<instance-id>/YYYY-MM.jsonl` (monthly file =
rotation; append-only; `.jsonl` invisible to md walks). `hash = sha256(prev +
canonical_json(record_without_hash))`; the chain head + monotonic seq are cached
in `.governance.sqlite receipts_head` so append is O(1) without re-reading the
tail, and truncation/rollback is detected on load (head ahead of file). Per-
machine partitioning means Obsidian sync never forks a single seq; cross-device
budget aggregation is a documented limitation, not silently wrong. Month
boundaries link the first record's `prev` to the prior month's head.

### D2 — Proportional emission, no plaintext
Emit only for: governed decisions (non-L6 participation), policy changes,
grants/revocations, deletions of governed material, budget warnings. A record
carries refs, source/released content hashes, byte sizes, level, redaction counts,
principal, audience, purpose, policy fingerprint, confirmation type — scope **ids
+ hashed labels**, never label text, never released content. Full-text logging is
an explicit per-scope opt-in. Ungoverned recall writes nothing.

### D3 — fsync policy split by criticality
Policy-mutation and deletion events flush + `os.fsync` (durability matters,
frequency is low). Disclosure/read events are buffered best-effort (protects find
latency; `query_log._append` semantics). This keeps the receipt-append budget
(< 3 ms amortized) off the hot recall path.

### D4 — Deletion events, not in the fan-out
`delete_file.py` emits a `{event: deletion, ...}` record after the successful
trash move + index fan-out (`:251-269`); `recover_from_trash` emits the inverse.
`index_sync.delete_after_remove` itself stays receipt-free (it is also called by
move/reconcile fan-outs with different semantics). No-op on empty policy.

### D5 — Chain verify as an audit category
A new `governance_receipts` entry in `audit.ALL_CATEGORIES` + a check block
(`audit.py:357-386` pattern) calling `receipts.verify_chain`, reachable via
`maintain_memory(mode="audit", categories=["governance_receipts"])` with zero new
surface. Detects edited lines, truncated tails, and broken month links; repairs
the head-cache forward when it lags the file after a crash.

## Risks / Trade-offs

- **Owner can destroy the log**: acknowledged and documented — "tamper-evident,"
  never "immutable." A break is *visible* (that is the honest behavior). Org
  assurance comes later via Hosted checkpoints.
- **Sidecar not restored on hosted portability**: `.governance.sqlite` (head
  anchor, HMAC key) is per-machine and does not travel; the in-vault chain does.
  A restore re-anchors from the chain tail — stated as a contract, not a surprise.
- **Label privacy**: scope labels can themselves be sensitive; store scope-ids +
  hashed labels so the log names no confidential project in the clear.

## Migration Plan

Additive. Absent `_Governance/`, nothing is written. `.governance.sqlite` gains
`receipts_head` via `PRAGMA user_version`. Existing vaults gain a chain only once
a governed event occurs.

## Open Questions

None blocking. Signed checkpoints and exportable reports are explicitly deferred.
