# Design: add-relation-acceptance-queue

## Context

All the parts exist; this change assembles them. Deterministic candidates:
`suggest_relations` (`epistemic_graph.py:512-543` — wikilink, frontmatter,
shared-source, embedding-proximity methods, `mutated: false`). Debt
measurement: `activation.py` findings with `next_actions` pointing at the
suggestion ops. Decisions: `review_state.py` (`ReviewStateStore.apply`,
fingerprint-keyed dismiss/snooze/reopen, atomic writes to
`<KB>/.review-state.json`). The write: Studio's `submitRelation`
(`app.v1.js:514-529`) → `edit_memory` heading-append of the canonical bullet
(`edit.py:98-172`; bullet contract per `markdown_relations.py:21-24`). What's
missing is only the batched queue view and a single governed accept step.

## Goals / Non-Goals

Goals: batch review of relation debt; durable rejections; one-call governed
accept; Studio panel; measurable coverage progress.

Non-Goals: model-generated candidates; auto-accept or accept-all; new
relation vocabulary; editing `epistemic_graph.py` (parallel-lane discipline —
the find-lane change owns that file); folding into the daily attention queue
(namespacing precedent from #198).

## Decisions

### D1 — New module `relation_queue.py`; `epistemic_graph.py` untouched
Queue assembly lives in a new module consuming the public
`suggest_relations` API per eligible page. Rationale: (a) file-disjoint from
the concurrently developed typed-find-lane change; (b) queue policy
(filtering, identity, counters) is review-domain logic, not graph-index
logic. Eligibility reuses the activation scan's eligible-page walk.

### D2 — Identity and fingerprint
`review_id = relation/<sha1(from_path|to_path|relation_type|method)[:16]>`,
prefix-namespaced (`exomem://review/relation/...`) so `VALID_ACTIONS` and the
store apply unchanged while never colliding with activation/attention items
(the #198 isolation rule). Fingerprint = the existing signal-fingerprint
recipe over (candidate triple, method, evidence anchor, source page
content_hash) — a material change to the evidence or page re-surfaces a
dismissed candidate, exactly like other queues.

### D3 — Filtering at read time, not decision time
Each read recomputes: drop candidates whose edge exists in the authored
`## Relations` of the source page (parse via `markdown_relations` — cheap,
already cached page model), whose target is a placeholder, or whose
fingerprint has an unexpired decision. Acceptance therefore needs no
queue-side bookkeeping: the authored edge itself removes the item (single
source of truth, no acceptance ledger to drift).

### D4 — Accept is a server-side compose of existing primitives
`op_accept_relation` (routed via `connect_memory(operation="accept-relation")`)
re-derives the candidate from the live signal, checks fingerprint equality,
then calls the same internal edit used by `op_edit_memory` heading-append
with the caller's `expected_hash`. Refusal reuses the existing drift error
contract (`REVIEW_ITEM_CHANGED` / hash-mismatch semantics). Rationale: the
Studio's current two-step client flow works but cannot be offered safely to
arbitrary MCP callers; one server-side step keeps the invariant "validate
against live state, then write once" regardless of client quality.

### D5 — Studio panel is a view over the same registry commands
Panel = `review_memory(mode="relation-queue")` list + per-item
`accept-relation` / `triage_memory` calls through the existing REST facade
(`studio/api.v1.js`), reusing `guardedWrite` and the audit-reason
requirement. No client-side ranking (Studio spec rule). The one-at-a-time
modal stays — it serves ad-hoc authoring outside the queue.

## Risks / Trade-offs

- **Queue size on a debt-heavy vault**: cap per read (e.g. top N pages by
  activation rank, counts reported like the capped-surfacing rule in the
  attention queue) — bounded response, no pagination state.
- **Fingerprint recipe drift**: reuse the existing helper rather than a new
  hash; test that dismiss survives an unrelated page edit but expires on a
  material one.
- **Concurrent accept vs. page edit**: covered by expected_hash refusal; the
  Studio already handles the retry UX.
- **Merge adjacency with the find-lane change**: both add ops to
  `commands.py`; textual conflict possible but semantic overlap none (the
  find lane does not touch commands.py). Merge order: find-lane first, this
  second (documented in tasks).

## Migration Plan

Additive ops and a new Studio asset version; no data migration.
`.review-state.json` schema unchanged. Ship dark (queue is read-only until
called), Studio panel behind the existing assets versioning.

## Open Questions

None blocking. Post-landing: bulk-accept UX (explicitly out of scope now),
tag-scoped queue slices, wiring queue counters into activation reporting.
