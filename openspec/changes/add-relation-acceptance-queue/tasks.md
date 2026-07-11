# Tasks: add-relation-acceptance-queue

## 1. Queue module (new src/exomem/relation_queue.py)

- [ ] 1.1 `build_queue(vault_root, *, limit_pages, ...) -> QueueResult`:
      walk activation-eligible pages (reuse activation's eligibility walk),
      call `suggest_relations` per page, assemble items with review refs
      (`exomem://review/relation/<sha1-16>`) and signal fingerprints (reuse
      the existing fingerprint helper from review_state usage).
- [ ] 1.2 Read-time filtering: authored-edge dedup (parse source page's
      `## Relations` via markdown_relations), placeholder-target drop,
      unexpired-decision drop (ReviewStateStore lookup).
- [ ] 1.3 Coverage counters aligned with activation denominators; capped
      surfacing with explicit dropped counts.
- [ ] 1.4 Red-first tests (tests/test_relation_queue.py): determinism on
      unchanged corpus; all three filters; counters; cap; `mutated: false`;
      no filesystem writes on read (assert vault tree hash unchanged).

## 2. Command surface (src/exomem/commands.py)

- [ ] 2.1 `review_memory(mode="relation-queue")` → relation_queue.build_queue
      (read-only registry op).
- [ ] 2.2 `connect_memory(operation="accept-relation")` → op_accept_relation:
      re-derive candidate, fingerprint equality check, expected_hash check,
      then the same internal heading-append edit path op_edit_memory uses;
      drift error contract on any mismatch; response includes the written
      bullet and new content hash.
- [ ] 2.3 `triage_memory`: relation-queue refs accepted by existing actions;
      namespace isolation (triaging a relation item never resolves
      activation/attention items — mirror the #198 isolation test).
- [ ] 2.4 Red-first tests: accept writes exactly one canonical bullet
      (byte-compare with a Studio-path write of the same candidate);
      fingerprint mismatch refuses; hash mismatch refuses; accepted item
      absent on re-read; dismissed item absent until fingerprint changes;
      MCP/REST/CLI all expose the new op (registry-generated surfaces test).

## 3. Studio panel (src/exomem/studio/)

- [ ] 3.1 Batched queue panel in app.v1.js: grouped by page, per-item
      Accept (audit reason required) / Dismiss / Snooze, via existing
      api.v1.js REST calls + guardedWrite fingerprint handling; panel
      refresh after actions; no client-side ranking.
- [ ] 3.2 Bump studio asset version per the existing versioning convention.
- [ ] 3.3 Extend tests/test_studio_governed_flows.py: queue render, accept
      round-trip, drift-refusal handling, triage round-trip.

## 4. Gates

- [ ] 4.1 `uv run python -m pytest -q` green.
- [ ] 4.2 `uv run python -m pytest tests/test_latency_gate.py -q` green
      (thresholds untouched; queue is not on the find path but the gate is
      the merge bar).
- [ ] 4.3 `uvx ruff check` clean on changed files.

## 5. Orchestrator-owned (NOT for the executor lane)

- [ ] 5.1 Docs: docs/review-studio.md queue section;
      docs/epistemic-inbox.md cross-reference; comparison doc improvement #1/#4
      status.
- [ ] 5.2 Scaffold SKILL.md: document relation-queue + accept-relation in the
      tool surface section (scaffold-no-leak test green).
- [ ] 5.3 Merge sequencing after the typed-find-lane change (commands.py
      adjacency).
