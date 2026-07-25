# Tasks: add-governance-kernel

## 1. Policy source + loader (src/exomem/governance/policy.py)

- [x] 1.1 Red tests `tests/test_governance_policy.py`: strict-YAML parse of
      scopes/rules/grants (unknown key → finding; bad ceiling → finding; empty
      dir → EMPTY singleton); fingerprint stability + change detection on a
      timestamp-preserving replace (`test_access.py:111` analogue);
      conflicted-copy sibling refuses compile.
- [x] 1.2 Implement `policy.load(vault_root)` mirroring
      `access._load_config`/`policy_fingerprint` (`access.py:59-135`): frozen
      dataclasses `Scope`/`Rule`/`StandingGrant`, findings-not-exceptions
      validation, dir stat-signature + content fingerprint cache, `EMPTY_POLICY`.

## 2. Sidecar + compiled snapshot (compile.py, store.py)

- [x] 2.1 Red test `tests/test_governance_store.py`: sidecar create with
      `sidecar_store` pragmas/meta + `PRAGMA user_version`; `compiled_policy`
      snapshot roundtrip keyed by fingerprint.
- [x] 2.2 Implement `compile.py` (normalized snapshot writer) + `store.py`
      skeleton; add `governance_sidecar_path` to `index_paths.py`.

## 3. Membership (membership.py)

- [x] 3.1 Red test `tests/test_governance_membership.py`: each selector kind
      (glob, project, tag, type, class, ref) + `exclude` selectors + memo
      invalidation on fingerprint/mtime change.
- [x] 3.2 Implement `membership.evaluate(page, policy)` against `ParsedPage`
      (reuse `structured_filters`/`find_corpus` idioms), bounded
      `(fingerprint, path, mtime_ns)` LRU memo.

## 4. Decision evaluator (decisions.py)

- [x] 4.1 Red test `tests/test_governance_decisions.py`: lattice truth-table
      (org cap dominates; grant elevates over standing min; default OPEN);
      purity (same inputs → same output, no IO); undeclared-purpose semantics.
- [x] 4.2 Implement `decide(...)` + `Decision` dataclass
      (`level, scope_ids, rule_ids, options, notice, bridge`) + session-grant
      read hook from `store`.

## 5. Fast path + corpus hygiene + scaffold

- [x] 5.1 Red test `tests/test_governance_overhead.py::test_empty_policy_short_circuit`
      (load → singleton, `decide_paths` returns a no-op map without opening the
      sidecar).
- [x] 5.2 Add `_Governance` to `find_corpus.EXCLUDED_DIR_NAMES` (`:20`); test an
      md planted there never surfaces in `find`.
- [x] 5.3 Add generic `src/exomem/_scaffold/_Governance/README.md`; keep
      `tests/test_scaffold_no_leak.py` green.

## 6. Command surface (inspection entry points)

- [x] 6.1 Register the internal kernel read leaves used later by
      `explain`/`simulate` (no user-facing tool this change); registry test.

## 7. Gates

- [x] 7.1 `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest -q
      tests/test_governance_policy.py tests/test_governance_store.py
      tests/test_governance_membership.py tests/test_governance_decisions.py
      tests/test_governance_overhead.py tests/test_scaffold_no_leak.py` green.
- [x] 7.2 `uv run python -m pytest tests/test_latency_gate.py -q` green (no
      governance on the find path yet — must be untouched).
- [x] 7.3 `uvx ruff check` clean; `openspec validate add-governance-kernel
      --strict` green.
