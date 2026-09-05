# Mutation results and recovery

## Mutation results and safe retries

Successful product mutations return a compact decisive terminal by default:
`ok: true`, `status: committed`, `mutated: true`, `path` or `paths`,
`request_id`, `receipt_id`, and `warnings_count` (plus a caller-supplied
`idempotency_key` when the surface supports one). A null receipt means that the
surface supplied no replay identity; it does not weaken the committed status.
When the write warned, `warnings` carries the texts — at most 8 entries of at
most 300 characters. `warnings_count` remains authoritative, so fewer entries
than the count means the rest were trimmed; ask for `response_detail="full"` to
see them all. An upload receipt reports its warnings as per-file rows under
`files` instead, and a mutation recovered from a portable receipt reports the
count alone because it retains no leaf content. A corpus write advisory includes
its review ref and complete 24-character signal fingerprint in its warning text;
triage binds to that exact fingerprint (dismiss requires a reason), and a stale
fingerprint simply lets the advisory re-emit when the counterpart changes or the
written page changes the detected signal class.
A compiled-note write may also carry `structure_suggestion`: an advisory
`kind`, a `strength` of `strong` or `moderate`, deterministic `reasons`, the
number of durable units in the group, and up to six recurring `cluster_terms`.
It is present only when the written page shows recurring durable material
outside its own declared scope, it reports nothing about any other page, and it
never affects `status`, `mutated`, or replay. Nothing is reorganised by the
runtime; acting on it is the agent's decision with the user.
Use `response_detail="full"` when existing leaf diagnostics are needed under
`diagnostics`. Use `response_detail="legacy"` only for temporary compatibility
with the former raw result. Response detail is presentation-only: changing it
does not change mutation identity, execute the leaf again, or alter a replayed
terminal.

A selected media process/retry request can finish without changing canonical
bytes: it returns `state: settled`, `status: settled`, `terminal: true`, and
`mutated: false`, with request identity and bounded `media_results`. This means
the request has completed, not that a new content commit or extraction has
completed. Inspect each item's state and error/remediation, and use the same
identity for exact replay. A failed-subset retry is a new request containing
only those failed paths.

Committed observation responses retain bounded `before_hash`, `after_hash`,
`unit_ref` and `removed_unit_ref` when their producer supplies them. Existing-page
edits retain the exact `after_hash` of their committed bytes. These are mutation
identities, not a promise that nobody has edited the page since: use the hash as
the next supported `expected_hash` and honor any stale-write refusal. Portable
receipt recovery may omit these fields; it never invents a missing identity.
Do not reread solely to confirm a successful commit. Use one bounded final
verification of the whole requested outcome, and extra reads only when content,
warnings or missing concurrency guards actually require them.

`graph_sync=pending` and `derived_sync=pending` describe asynchronous work, not a
failed canonical write. Continue independent work and ordinary recall; do not
run maintenance merely to remove the pending label. A graph-dependent query can
still require current edges and return an explicit freshness refusal.

`MUTATION_WARMING`, `MUTATION_BUSY`, `MUTATION_ACKNOWLEDGEMENT_PENDING`, and
`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN` remain errors, not successful
terminals. Preserve the same mutation identity and unchanged payload when
following their remediation: wait before retrying a warming or busy call; retry a pending
call only with the same identity; do not submit a new identity after a
committed-uncertain result—reconcile and retry only as instructed.

On MCP these expected refusals arrive as normal tool content with top-level
`success: false`; inspect the structured `error` rather than treating it as a
transport failure. A `receipt_id` is diagnostic and is not a transferable
cross-session replay key. `coordination_status` probes this vault's local OS
mutation boundary: `verified: true` binds the safe holder fields to the current
lock generation, while `verified: false` means the external holder is real but
cannot be safely attributed. Never infer vault content or identity from status.
