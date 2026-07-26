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

Goals: a proportional, plaintext-free, tamper-evident receipt foundation for
governed egress and existing token/content lifecycle events; deletion evidence
that outlives the source without its plaintext; a reusable crash-reconcilable
critical-event protocol for the dependent governance-tools change; cheap,
read-only chain verification in the existing audit surface; honest vocabulary.

Non-Goals: policy authoring, signed checkpoints, Hosted org countersignatures,
exportable compliance reports, budgets, and plaintext logging (all later or
never). Ordinary ungoverned L6 recall produces no receipt.

## Decisions

### D1 — One chain per machine, sidecar-anchored
Records live in `_Governance/events/<instance-id>/YYYY-MM.jsonl` (monthly file =
rotation; append-only; `.jsonl` invisible to md walks). The canonical payload is
UTF-8 JSON with sorted keys, compact separators, non-ASCII preserved, non-finite
numbers refused, and the trailing newline excluded. `hash = sha256(prev_hash_bytes
|| canonical_json(record_without_hash))`, with 32 zero bytes at genesis. The
sidecar keeps two positions: a durable head/seq for the latest fsync'd JSONL
prefix and an observed head/seq cache for the latest flushed append. A
process-safe per-instance append lock serializes read-path emitters that do not
hold the mutation lease. Under that lock, no append allocates a sequence until a
bounded seek/read of the actual final record agrees with an adopted observed
head. A critical append flushes and fsyncs JSONL before advancing both heads. If
file fsync wins but the sidecar update does not, recovery verifies the
file-ahead suffix, fsyncs it, and promotes it before another append. A read-path
append flushes JSONL and advances only the observed cache; after power loss,
recovery may discard a missing observed suffix only when the remaining file is
a valid extension of the durable head. A tail behind or divergent from the
durable head is truncation/tamper and stays blocked. This makes recovery
O(delta), steady-state append O(1), and prevents sequence reuse or chain forks
after either write-order crash. Per-machine partitioning means vault sync never
forks a single seq. Month boundaries link the first record's `prev` to the prior
month's head.

`events/**` and `deletion-tombstones/**` are reserved operational namespaces.
Policy discovery, policy fingerprints, policy-cache dependencies, unknown-file
findings, and generic policy conflict-copy rejection exclude them. Receipt
conflict copies instead make receipt append fail closed and surface in receipt
audit. The separate pending-policy marker is also excluded from policy sources
but checked first by the dependent governance-tools loader guard.

### D2 — Proportional emission, event-specific schemas, no plaintext
Emit only for governed decisions (non-L6 participation, including the always-on
credential block), withhold-token mint/redeem, and governed deletion/recovery.
Every record has a versioned common envelope plus a validated event-type payload;
the union includes refs, source/released content hashes, byte sizes,
level/outcome summaries, redaction counts, principal, audience, purpose, policy
fingerprint, confirmation type, and scope ids plus keyed label digests. Each type
requires only applicable fields: a credential block without policy fabricates no
policy/scope values, and mixed aggregates use typed outcomes. It never carries
released content, credentials, token bytes, or human label text. There is no
full-text opt-in. Ordinary ungoverned L6 recall writes nothing.

Critical events derive a deterministic id from their operation identity. A
top-level read mints a fresh boundary/event id once at invocation entry and
retains it only across internal append/anchor retries in that invocation. It is
never derived only from arguments or a transport request id: an external
CLI/REST/MCP retry is a new disclosure attempt and gets a new boundary id.

### D3 — Critical events use receipt-first intent + terminal phases
State-changing events owned by this foundation (token consumption,
deletion/recovery) call `begin_event` before the state change. It appends and
fsyncs an `intent` carrying the prior/target fingerprints and deterministic
event id. Success appends and fsyncs `committed`; ordinary refusal appends
`aborted`; a crash leaves an unresolved intent that reconcile classifies from
current state without replaying the mutation. Terminal appends are idempotent by
event id. The dependent governance-tools change reuses this protocol before any
authorization-affecting state becomes visible.

Disclosure/read decisions append synchronously under the receipt lock and flush
the userspace buffer before a governed representation returns, but do not fsync
on the hot path. An append error fails closed with a content-free retryable
service error; it never returns the unreceipted governed payload. These receipts
attest the representation Exomem selected/assembled at its boundary, not client
receipt or transport completion, and do not claim tail survival across whole-
system power loss. A governed same-process micro-gate compares equivalent
controlled responses rather than relying only on the ungoverned latency gate.

### D4 — Collect outcomes at reductions; emit exactly once after projection
There is no one current leaf covering search, page, graph, structure, overview,
downloads, frames, and prompt/resources. Each reduction/projection adapter
therefore contributes a content-free `DisclosureOutcome` to a request-scoped
collector. Nested aliases such as `ask_memory` calling `op_find` contribute but
do not emit; only the outer owning boundary emits after its final representation
is selected. The event contains an `outcomes` array for mixed item/level results,
with a type-specific schema rather than pretending every event has one level or
policy fingerprint. The universal `postfilter` remains side-effect-free because
MCP deliberately runs it twice.

Coverage is derived from content-returning operation/mode branches and registered
reduction adapters, not only top-level command names; non-command
download/frame/prompt routes are explicit. A successful governed top-level
boundary emits exactly one event. The second MCP pass emits zero. Streaming
routes record `release_authorized`, never claim downstream delivery.

### D5 — Tombstones make deletion/recovery crash states exact
`delete_file.py` and `delete_directory.py` capture refs, hashes, exact source and
trash locations, and a batch manifest before mutation. After durable intent they
fsync content-free tombstones, then move and clean metadata, semantic rows, CLIP
rows, and scene derivatives. Every egress operation/mode adapter and non-command
download/frame/prompt/resource route consults tombstones before returning a stale
derivative. Tombstone coverage is bound to the same branch/adapter registry as
receipt outcomes, with explicit non-command entries and a mutation gate for a
missing check. The logical prior fingerprint is exact source content present and
exact trash target absent; the target is source absent and the exact trash
manifest present. The pending tombstone is the fail-closed transaction guard,
not evidence that the move itself occurred.

On restart, `maintain_memory(mode="reconcile")` heals ordinary derived state
first. Exact prior then clears the pending deletion tombstone and aborts; exact
target keeps the deletion tombstone until zero searchable residue is verified,
then commits. Any other placement/hash combination stays blocked with tombstones
in force. Reconcile may repeat idempotent derived cleanup but never the semantic
move. `recover_from_trash` uses the inverse: a matching governed deletion
tombstone (or current governance) keeps the exact restored content invisible
through reindex; terminal evidence is fsync'd before the tombstone is removed
and recovery activates. Its exact prior is the captured trash manifest present,
source absent, and deletion tombstone active; its exact staged target is source
restored at the captured hash, trash target absent, required metadata/semantic/
CLIP/scene derivatives matched, and tombstone still active. Exact prior aborts,
exact staged target commits and removes the tombstone, and a third state blocks.
A later policy edit therefore cannot erase the evidence link.

`index_sync.delete_after_remove` stays receipt-free because move and reconcile
also call it with different semantics. Only the deletion/recovery leaf owns the
critical event. Ordinary ungoverned deletion with no governed-deletion lineage
is a receipt no-op.

### D6 — Audit verifies; reconcile repairs
A new `governance_receipts` entry in `audit.ALL_CATEGORIES` + a check block
calling `receipts.verify_chain`, reachable via
`maintain_memory(mode="audit", categories=["governance_receipts"])`, detects
edited lines, truncated tails, broken month links, anchor lag, and unresolved
critical intents without writing. The existing write-capable
  `maintain_memory(mode="reconcile")` route calls `receipts.reconcile`; with
  `dry_run=True` it reports the exact proposed repairs without changing JSONL,
  tombstones, derivatives, or sidecar. A write run may promote a verified suffix,
  reset only a non-durable observed tail, or append an idempotent terminal after
  operation-specific exact-state classification. Any third state remains blocked
  for manual review.

## Risks / Trade-offs

- **Owner can destroy the log**: acknowledged and documented — "tamper-evident,"
  never "immutable." A break is *visible* (that is the honest behavior). Org
  assurance comes later via Hosted checkpoints.
- **Sidecar not restored on hosted portability**: `.governance.sqlite` (head
  anchors, HMAC key) is per-machine and does not travel; the in-vault chains do.
  A sidecar-less restore mints a new instance id and genesis chain. Prior
  instance directories remain independently verifiable history and are never
  merged or silently adopted as the new machine's chain.
- **Label privacy**: scope labels can themselves be sensitive; store scope-ids +
  keyed label digests so the log names no confidential project in the clear and
  does not expose low-entropy labels to an offline dictionary attack.
- **Un-fsynced egress tail**: read-path receipts are synchronously appended but
  do not claim power-loss-durable delivery evidence. Critical state transitions
  do. This boundary is explicit rather than calling telemetry immutable or a
  downstream delivery acknowledgement.

## Migration Plan

Additive. Ordinary ungoverned L6 recall writes nothing; an always-on credential
block may bootstrap only the reserved operational events path and sidecar even
when no policy exists. `governance/store.py` exclusively owns one monotonic
`PRAGMA user_version` migration sequence: receipts migrate v1→v2, the dependent
tools change migrates v2→v3, and every opener preserves later versions. The
sidecar gains
durable/observed heads and idempotent critical-event metadata. Existing vaults
gain a chain only once a governed event occurs. JSONL remains the evidence
authority. Operational events/tombstones do not become policy inputs or
invalidate the policy cache.

## Open Questions

None blocking. Signed checkpoints and exportable reports are explicitly deferred.
