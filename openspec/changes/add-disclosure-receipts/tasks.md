# Tasks: add-disclosure-receipts

## 1. Chained log (src/exomem/governance/receipts.py)

- [x] 1.1 Red test `tests/test_governance_receipts.py`: canonical append +
      `verify_chain`; edited-line, truncated-tail, broken-month, invalid-number,
      durable/observed head divergence; JSONL-fsync-before-sidecar crash;
      buffered-suffix power loss; stale-anchor append refusal; no duplicate seq
      or fork; concurrent same-instance appends serialize.
- [x] 1.2 Implement the process-safe per-instance monthly JSONL chain with exact
      canonical JSON/hash rules, deterministic event ids, keyed label digests,
      and separate durable/observed heads in `.governance.sqlite receipts_head`.
- [x] 1.3 Red crash-injection tests for `begin_event` + idempotent
      `committed`/`aborted` terminal phases: before state change, after state
      change, after terminal append, and a neither-prior-nor-target ambiguity.
- [x] 1.4 Implement receipt-first critical events and read-only verification;
      keep JSONL authoritative and the sidecar recovery-only.

## 2. Existing egress and token wiring

- [x] 2.1 Red tests cover once-only, plaintext-free outcomes for search,
      direct page reads, graph, overview/count reductions, structure/review
      payloads, downloads, video frames, and MCP prompt/resources; prove MCP's
      second filter pass and nested alias calls emit nothing independently,
      mixed levels use typed `outcomes`, and ordinary ungoverned L6 recall writes
      nothing. Inject an append failure and prove the governed payload fails
      closed instead of leaving unreceipted. Prove internal anchor retry reuses
      the invocation's minted boundary id while external CLI/REST/MCP retries
      mint new ids even for identical arguments/shared transport ids.
- [x] 2.2 Implement a content-free `DisclosureOutcome` collector in
      `governance/egress.py` and explicit final-representation adapters at every
      existing operation/mode reduction and non-command boundary. Keep nested
      aliases and `postfilter` side-effect-free; streaming routes emit
      `release_authorized` only. Add a mutation test showing a new mode inside an
      existing command fails registry-derived coverage.
- [x] 2.3 Red tests cover withhold-token mint and receipt-first consume events,
      distinct event types/causation ids for token redemption versus future
      grant creation, and always-on credential blocking without policy.
- [x] 2.4 Wire token mint/redeem in `governance/tokens.py`; never record token
      bytes, credentials, released content, or human scope labels.
- [x] 2.5 Define and validate common-envelope plus event-specific schemas for
      disclosure/credential/token/deletion/critical records; required fields are
      type-correct and inapplicable policy/scope fields remain optional.
- [x] 2.6 Red tests prove events/tombstones do not change policy fingerprints or
      warm-cache identity; cold load ignores them; a conflicted event file leaves
      policy enforcement active but makes receipt append fail closed and receipt
      audit report the conflict. Keep manual policy YAML behavior unchanged.

## 3. Deletion events

- [x] 3.1 Red tests cover governed file deletion, directory deletion containing
      multiple governed items, and inverse recovery after the governing policy
      changes or disappears. Inject crashes before tombstone, after tombstone,
      after move/restore, after each metadata/semantic/CLIP/scene fan-out, and
      after terminal. Exercise tombstoned stale content across every registered
      operation/mode adapter plus direct-read, download, frame, and
      prompt/resource routes; mutation-test a missing gate so coverage fails.
      Prove residue and ambiguous placement remain hidden. Ordinary ungoverned
      deletion is a receipt no-op.
- [x] 3.2 Capture refs/hashes/exact source-trash placement and batch manifest;
      fsync content-free tombstones and use the critical-event protocol around
      `delete_file.py`, `delete_directory.py`, and `recover_from_trash.py`.
      Reconcile derived state before receipt classification and may repeat
      idempotent cleanup but never the semantic move/restore; keep
      `index_sync.delete_after_remove` receipt-free.

## 4. Audit and reconcile integration

- [x] 4.1 Red tests prove `governance_receipts` audit reports tamper, anchor lag,
      and unresolved intents without writing; explicit reconcile repairs only an
      intact lagging anchor or an exact prior/target match and is idempotent.
      `dry_run=True` reports exact repairs while leaving JSONL, tombstones,
      derivatives, and sidecar byte-identical.
- [x] 4.2 Add `governance_receipts` to `audit.ALL_CATEGORIES` + a check block
      calling `receipts.verify_chain`; wire `receipts.reconcile` only into the
      existing write-capable `maintain_memory(mode="reconcile")` route.
- [x] 4.3 Make `governance/store.py` the sole migration owner: migrate v1→v2,
      preserve newer versions in every opener, and test that the dependent v3
      schema is never reset or rerun.

## 5. Gates

- [x] 5.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_governance_receipts.py tests/test_governance_egress.py
      tests/test_governance_tokens.py tests/test_audit.py` green; deletion and
      recovery scenarios live in `tests/test_governance_receipts.py`.
- [x] 5.2 Full suite green. Existing `tests/test_latency_gate.py` remains green.
      A governed same-process paired micro-gate runs 30 warm-ups and 200
      alternating samples for (a) a fixed 10-hit mixed-level search and (b) a
      100-entry structure reduction, embeddings disabled on a local temp vault.
      Against the same projection with a test receipt sink that validates and
      serializes but does not write, real JSONL append overhead SHALL be median
      <= 3 ms and p95 <= 8 ms for each case; print distributions on failure and
      do not change the existing ungoverned threshold file.
- [ ] 5.3 `uvx ruff check` clean; `openspec validate add-disclosure-receipts
      --strict` green.
