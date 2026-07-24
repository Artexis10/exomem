# Tasks: add-disclosure-receipts

## 1. Chained log (src/exomem/governance/receipts.py)

- [ ] 1.1 Red test `tests/test_governance_receipts.py`: append + `verify_chain`;
      tamper detection (edited line, truncated tail, broken month link);
      head-cache repair-forward after a simulated crash (head behind file).
- [ ] 1.2 Implement per-instance monthly JSONL, `hash = sha256(prev + canonical)`,
      head + seq anchored in `.governance.sqlite receipts_head`.

## 2. Emission wiring

- [ ] 2.1 Red test: policy mutation (commit/grant/revoke/undo) emits fsync'd
      receipts; disclosure/withhold emits buffered receipts; token mint/redeem
      recorded. No plaintext; scope ids + hashed labels only.
- [ ] 2.2 Wire emission in `governance/tool.py`, `governance/egress.py`,
      `governance/tokens.py`.

## 3. Deletion events

- [ ] 3.1 Red test `test_delete_emits_receipt` (+ inverse on recover); no-op on
      empty policy.
- [ ] 3.2 Emit in `delete_file.py` after trash move + fan-out (`:251-269`);
      inverse in `recover_from_trash.py`.

## 4. Audit integration

- [ ] 4.1 Red test `tests/test_audit.py::test_governance_receipts_category`.
- [ ] 4.2 Add `governance_receipts` to `audit.ALL_CATEGORIES` + a check block
      calling `receipts.verify_chain`; reachable via `maintain_memory(mode="audit")`.

## 5. Gates

- [ ] 5.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_governance_receipts.py tests/test_audit.py` green.
- [ ] 5.2 `uv run python -m pytest tests/test_latency_gate.py -q` green (receipt
      append off the hot path; buffered disclosure events).
- [ ] 5.3 `uvx ruff check` clean; `openspec validate add-disclosure-receipts
      --strict` green.
