# Tasks — trim-compact-bootstrap-and-admit-destination-choice

## 1. Red-first

- [x] 1.1 Compact-carriage test written and captured failing verbatim on the
      untouched base; budget test with `-W error::UserWarning` captured failing
      on base (24-byte headroom) as the trim's red evidence.

## 2. Trim

- [x] 2.1 Redundancy-first cuts in `op_bootstrap` compact prose per design D2;
      every cut argued in a new dated `#:` log entry in
      `tests/test_bootstrap_compact_budget.py`; pins moved with text, none
      loosened or deleted.

## 3. Admission

- [x] 3.1 `destination_choice` teaching present in the compact payload
      (condensed allowed, both halves of the rule intact per D3); full and
      diagnostics wording untouched; the S4 compact byte-identical pin
      replaced by the carriage pin in the same delivery.

## 4. Gates

- [x] 4.1 `tests/test_bootstrap_compact_budget.py` green UNMODIFIED in its
      constants (ceiling 61,400, ratio 0.15, warning 512) and green under
      `-W error::UserWarning`; paste `_size` for compact/full/diagnostics —
      compact ≤ 60,888.
- [x] 4.2 `tests/test_epistemic_bootstrap_contract.py` and every bootstrap /
      prominence / scaffold suite that reads the payload green; scaffold
      no-leak suite green.
- [x] 4.3 `uvx ruff check src/exomem --select F`; `npm exec --yes
      @fission-ai/openspec@1.10.0 -- validate --all --strict`; `git status`
      proof of no tool-surface pin movement.
- [x] 4.4 Mutation proof in a scratch copy under /tmp/claude-1000/**: reverting
      the trim (restoring any one cut passage) while keeping the clause trips
      the promoted-warning budget run — the margin is load-bearing.
