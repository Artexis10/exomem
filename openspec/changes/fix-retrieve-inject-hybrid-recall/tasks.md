## 1. Hook

- [x] 1.1 Red-first tests: hybrid mode on both rungs, marked REST envelope, service.env key fallback with quote un-escaping and BOM, loopback binding of a file-sourced key, shared budget, late cooldown stamp, whole-line stub bound, lane logging (`tests/test_retrieve_inject.py`).
- [x] 1.2 Implement in `src/exomem/_hooks/exomem_retrieve_nudge.py`; keep `plugins/claude-code/hooks/exomem_retrieve_nudge.py` byte-identical.
- [x] 1.3 Reproduce on a live local service with a 3 KB ticket prompt: original hook emits no stub block; fixed hook emits three stubs over REST (1.2 s) and via the CLI (2.5 s); a file-sourced key with a non-loopback `EXOMEM_HOST` sends nothing to that host.
- [x] 1.4 Run `tests/test_retrieve_inject.py`, `tests/test_prominence.py` and `scripts/validate-public-artifacts.py --repository` green.

## 2. Docs and neighbours

- [x] 2.1 Update the QUICKSTART inject paragraph and the benchmark `injection_ladder.py` notes.
- [x] 2.2 Author-independent review of the actual diff, with the reproduction rerun by the reviewer; correction round applied and rechecked.

## 3. Follow-ups

- [ ] 3.1 `install-hook --check` reports which inject lane is reachable (second half of #1142).
- [ ] 3.2 After merge, synchronize this delta into `openspec/specs/retrieve-inject-hook/spec.md` and archive the change.
