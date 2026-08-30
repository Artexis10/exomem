# Tasks — extend due-state carriage to the operation leaves

Every test lands red first (verbatim failing output recorded before the
implementation, then green).

## 1. Carriage

- [x] 1.1 Wire the terminal's due-state admission for each enumerated
  invocation (design D2), reusing the `due_state.block_for_write` family
  behind the `due_state_advisory` disclosure boundary. Red-first per leaf: a
  committing invocation whose writes change the counts carries EXACTLY ONE
  block reflecting the post-batch projection; outcome keys byte-identical
  otherwise. Each leaf is independently shippable behind its own test.
- [x] 1.2 Batch-once, change-only, and ledger pins: a multi-write apply
  emits one block recorded once in the emission ledger; a committing
  invocation with unchanged totals emits none. Red-first against the ledger.
- [x] 1.3 Negative pins: no-commit invocations (clean-vault `fix
  dry_run=false`, already-valid media, `process_media` `retry`), read-only
  and dry-run invocations (scan-only adopt, default dry-run `fix`, status
  and preview actions), and the legacy response detail all carry no block.
  Red-first.
- [x] 1.4 Failure isolation: unreadable review state yields no block while
  the operation completes with its terminal unchanged; a partially failed
  invocation that committed at least one write still carries under
  change-only. Red-first both halves.
- [x] 1.5 Projection-delta settlement per design D3: wire the batch-delta
  path for `maintain_memory` mutating modes or record evidence that the
  rebuild path covers them; pin that the served block reflects the
  post-batch projection, not a stale read. Red-first with a
  mid-batch-changing fixture.
- [x] 1.6 Pre-wiring payload check per leaf: the terminal's compact rebuild
  preserves the leaf's response payload (adoption-run document, maintain
  summaries, media job payload including the `state` key collision). A
  payload the terminal would drop is a BLOCKER escalated as its own change —
  never silently worked around. Record the per-leaf result here.

## 2. Bench and contract closure

- [x] 2.1 Invert — do not delete — the two zero-carrier tripwire pins in
  `tests/test_due_state_emission_capture.py` (their red run against the old
  expectation is this task's red-first evidence); update the f23 driver
  docstring's zero-carrier inventory; record the f23
  `counter_emission_not_repeated_per_write` flip `unsupported` → decided in
  this file. Record f26's before/after when amendment sequence 2 activates
  (nothing publishes meanwhile). No family, assertion, predicate, gate, or
  OpKind changes (design D5).
- [x] 2.2 Response-contract check: if any of the five leaves' recorded
  contract or packaged digest moves, follow the documented two-phase rollout
  and record it here; otherwise record that no contract moved.
- [x] 2.3 `openspec validate --all --strict` green; scoped suites green; full
  suite at the completion boundary attributed against the origin/main
  baseline.

---

# Evidence

## 1.6 — pre-wiring payload check (ran FIRST, before any wiring)

Measured at `7a5235e0` (branch point, no implementation applied) with a
temporary probe that spied on `writer_lease.project_terminal`, driving each
leaf through `writer_lease.invoke_command` at all three response details.

**The finding that decides this task: all nine enumerated invocations already
reach the committed terminal today.** `mark_active_mutation_committed()` fires
inside `vault.batch_atomic_write` on every one of them, so `invoke_leaf`
already wraps each leaf in `committed_terminal(...)` and `project_terminal`
already rebuilds each response. This change routes nothing new through the
terminal — it only attaches one advisory key to a leaf that was already being
projected.

| Leaf / invocation | reaches committed terminal | compact preserves payload | `full` | `legacy` |
|---|---|---|---|---|
| `adopt_vault` copy-as-sources ×12 | yes | no — envelope + `paths` only | yes, verbatim under `diagnostics` | yes, verbatim |
| `adoption_studio` `apply` | yes | no — envelope + `paths` only | yes, verbatim under `diagnostics` | yes, verbatim |
| `maintain_memory` `fix dry_run=false` | yes | no — envelope only | yes, verbatim under `diagnostics` | yes, verbatim |
| `maintain_memory` `reconcile` | yes | no — envelope + `graph_rebuild_*` | yes, verbatim under `diagnostics` | yes, verbatim |
| `maintain_memory` `backfill-ids dry_run=false` | yes | no — envelope only | yes, verbatim under `diagnostics` | yes, verbatim |
| `maintain_memory` `structured-files apply=true` | yes | no — envelope only | yes, verbatim under `diagnostics` | yes, verbatim |
| `preserve_artifacts` | yes | **yes** — `files` and `summary` survive via `_artifact_receipt_projection` | yes | yes, verbatim |
| `process_media` `process` | yes | partial — `path` survives via `_path_projection`; the media `state` is shadowed | yes, verbatim under `diagnostics` | yes, verbatim |
| `adoption_studio` `apply-proposal` | yes | no — envelope + `paths` only | yes, verbatim under `diagnostics` | yes, verbatim |

Representative measurement, `adopt_vault` copy-as-sources ×12:

```
  detail=compact  reached_committed_terminal=True
    leaf payload keys : ['available_packs', 'copy', 'governance', 'graph_sync', 'graph_sync_checkpoint', 'graph_sync_code', 'graph_sync_remediation', 'implemented_modes', 'mode', 'next_actions', 'overview', 'pack_schema', 'pack_suggestions', 'planned_modes', 'refs', 'scope_note', 'summary', 'write_contract']
    response keys     : ['graph_sync', 'graph_sync_checkpoint', 'graph_sync_code', 'graph_sync_remediation', 'mutated', 'ok', 'paths', 'receipt_id', 'request_id', 'state', 'status', 'terminal', 'warnings_count']
  detail=full     reached_committed_terminal=True
    response keys     : ['diagnostics', 'graph_sync', ...]
    diagnostics keys  : ['available_packs', 'copy', 'governance', ..., 'summary', 'write_contract']
  detail=legacy   reached_committed_terminal=True
    response keys     : ['available_packs', 'copy', 'governance', ..., 'summary', 'write_contract']
```

The `state` collision, measured on `process_media`: the media job's own
`state` (`pending`) is not dropped into nowhere — the compact `state` is the
envelope's closed mutation state (`committed`), and the media `state` reaches
the caller under `diagnostics` at `full` and verbatim at `legacy`.

```
  detail=compact  leaf keys: [..., 'job_id', 'media_type', 'operation', 'path', 'sidecar_path', 'state']
                  response keys: [..., 'ok', 'path', ..., 'state', 'status', 'terminal', 'warnings_count']
  detail=legacy   response keys: [..., 'index_refreshed', 'job_id', 'media_type', 'operation', 'path', 'sidecar_path', 'state']
```

**Verdict: NO BLOCKER on any leaf; all nine wire.** The compact narrowing is
not a payload the terminal *drops* — it is the canonical projection this
repository specifies, `openspec/specs/mutation-terminal-contract/spec.md`:

> **Requirement: Compact Success Is The Default Projection** — Committed
> product mutations SHALL return a compact default projection led by `ok`,
> `status`, `mutated`, primary `path`, original `request_id`, stable receipt
> identity, and `warnings_count`. `response_detail="full"` SHALL add the
> complete leaf result under `diagnostics`; `response_detail="legacy"` SHALL
> return the pre-change raw leaf result during the compatibility window.

Every leaf's complete payload was measured present under `diagnostics` at
`full` and verbatim at `legacy`, so nothing is lost and nothing is being
worked around. What this change adds is one advisory key on a response shape
that was already being rebuilt; `_without_advisory_due_state` strips it from
the leaf before `diagnostics`/`legacy` see it, so no leaf payload moves.

One consequence this check DID settle, and which the implementation must
honour: `maintain_memory structured-files apply=true` returns a receipt whose
key set is closed (`mutation_terminal.valid_structured_files_receipt` uses
exact set equality, and `tests/test_structured_file_migration.py:378` pins it
on the raw leaf return). Attaching the block unconditionally would invalidate
that receipt and break `writer_lease`'s replay detection at
`writer_lease.py:3044`. The carrier is therefore gated on
`writer_lease.active_mutation_committed()`, which is false for a replay (a
replay commits nothing) and false for a direct leaf call outside a mutation
trace — so both the receipt contract and the replay path stay intact, and D1's
"no committed write, no carriage" rule is enforced by the same gate.

## 1.1–1.5 — red-first

`tests/test_due_state_bulk_carriers.py`, run against the branch point with no
implementation applied:

```
PYTHONPATH=<worktree>/src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest \
  tests/test_due_state_bulk_carriers.py -q --tb=line --basetemp=/dev/shm/pytest-bulk-red3

E   AssertionError: {'graph_sync': 'pending', 'graph_sync_checkpoint': 'e37b5fb930...', ...}
tests/test_due_state_bulk_carriers.py:212: AssertionError
E   AssertionError: {'files': [{'content_type': 'image/png', 'file_id': 'file-one', ...}], 'mutated': True, 'ok': True, 'path': 'Knowledge Base/Evidence/case/raw/artifact.png', ...}
tests/test_due_state_bulk_carriers.py:258: AssertionError
E   KeyError: 'due_state'
tests/test_due_state_bulk_carriers.py:483: KeyError: 'due_state'
E   KeyError: 'due_state'
tests/test_due_state_bulk_carriers.py:494: KeyError: 'due_state'
E   KeyError: 'due_state'
tests/test_due_state_bulk_carriers.py:505: KeyError: 'due_state'
=========================== short test summary info ============================
FAILED tests/test_due_state_bulk_carriers.py::test_adopt_vault_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_adoption_studio_apply_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_maintain_fix_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_maintain_reconcile_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_maintain_backfill_ids_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_preserve_artifacts_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_process_media_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_structured_files_apply_carries_one_block
FAILED tests/test_due_state_bulk_carriers.py::test_the_block_leaves_the_operation_outcome_keys_untouched
FAILED tests/test_due_state_bulk_carriers.py::test_a_twelve_write_adopt_records_one_emission_for_twelve_writes
FAILED tests/test_due_state_bulk_carriers.py::test_a_committing_invocation_with_unchanged_totals_carries_nothing
FAILED tests/test_due_state_bulk_carriers.py::test_a_partially_failed_invocation_that_committed_still_carries
FAILED tests/test_due_state_bulk_carriers.py::test_the_adopt_block_counts_the_batch_it_just_committed
FAILED tests/test_due_state_bulk_carriers.py::test_the_fix_block_counts_the_pages_the_pass_just_rewrote
FAILED tests/test_due_state_bulk_carriers.py::test_the_reconcile_block_counts_a_page_written_out_of_band
15 failed, 7 passed
```

The seven that pass red are the 1.3/1.4 negative pins, which are vacuous
before the carrier exists and only start defending once it does: they must
still be green after the implementation, and that is what makes them worth
keeping.

The red-evidence test recorded above as
`test_the_adopt_block_counts_the_batch_it_just_committed` shipped under its
final name `test_the_adopt_block_reports_the_projection_after_the_batch`: its
corrected measured meaning is the post-batch served projection, rather than a
claim that the adopted Source pages changed the due totals. No historical rerun
is claimed for that rename.

## 1.1–1.5 — green, and the mechanism-removal proof

```
PYTHONPATH=<worktree>/src EXOMEM_DISABLE_EMBEDDINGS=1 uv run python -m pytest \
  tests/test_due_state_bulk_carriers.py -q --basetemp=/dev/shm/pytest-bulk-green3
.......................                                                  [100%]
23 passed in 7.82s
```

Green alone would not distinguish a carrier that works from a pin that stopped
looking, so each of the two mechanisms this change adds was removed and the
suite rerun.

Removing the `fix` batch-delta wiring (task 1.5) — the stale-projection pin is
the only thing that notices, which is exactly what it was written for:

```
tests/test_due_state_bulk_carriers.py:565: assert 1 == (1 + 1)
FAILED tests/test_due_state_bulk_carriers.py::test_the_fix_block_counts_the_pages_the_pass_just_rewrote
1 failed, 22 passed
```

Removing the commit gate from `_carrying_due_state` (design D1) — all five
negative pins fall, and the leaked payload shows what the gate is really
protecting: a dry-run preview carrying an advisory it must never carry, with
the server-internal `_vault` absolute path raw on the wire, because a
non-committing leaf has no terminal to validate and strip it.

```
tests/test_due_state_bulk_carriers.py:452: AssertionError: {'dry_run': True, 'due_state': {'_vault': '/dev/shm/pytest-bulk-rm2/…/vault', 'categorie…
FAILED tests/test_due_state_bulk_carriers.py::test_a_clean_vault_fix_carries_nothing
FAILED tests/test_due_state_bulk_carriers.py::test_already_valid_media_carries_nothing
FAILED tests/test_due_state_bulk_carriers.py::test_process_media_retry_carries_nothing
FAILED tests/test_due_state_bulk_carriers.py::test_scan_only_adopt_carries_nothing
FAILED tests/test_due_state_bulk_carriers.py::test_the_default_dry_run_fix_carries_nothing
5 failed, 18 passed
```

### 1.5 — the projection-delta settlement per leaf (design D3)

| Mutating mode | Delta path | Why |
|---|---|---|
| `adopt_vault`, `adoption_studio` `apply` | `_apply_batch_deltas` (already wired) | unchanged by this change |
| `maintain_memory` `reconcile` | **rebuild proves it** — `op_reconcile` already calls `due_state.reconcile`, a full recompute | strictly stronger than a per-path delta, so no batch deltas were added |
| `maintain_memory` `fix` | **wired** — `_apply_batch_deltas(vault_root, _audit_fix_paths(report))` | `fix` has no recompute of its own; without this the block describes the vault as it was before the pass |
| `maintain_memory` `backfill-ids` | **wired** — `_apply_batch_deltas` over `report["updated"]` | same reason |
| `structured-files`, `preserve_artifacts`, `process_media` | none needed | renames, Evidence blobs and transcript sidecars author no predictions, questions, experiments or supersession pointers |

Two fixture facts were measured rather than assumed, and both changed how the
negative pins are built:

- **`maintain fix dry_run=false` on the canonical fixture vault is not a
  no-write invocation.** `audit_fix` composes a sub-index refresh into the same
  batch as its content repairs and commits it without counting it in
  `files_rewritten` (`audit_fix.py:565-572`); on that vault the refresh recurs
  every pass. A repeat `fix` there really does commit a governed write and
  really should carry. The clean-vault pin therefore runs on a bootstrapped
  vault whose indexes are current, and asserts the commit count is zero instead
  of inferring it from the absent block.
- **`copy-as-sources` adds zero due items.** Twelve adopted files move the
  ledger's write count by twelve and the due total by nothing, because they land
  as `type: source` pages with the prediction quoted inside a fenced Capture
  block. The block a bulk adopt delivers is therefore a first-of-session
  delivery, not a count change. This is pinned in
  `test_the_adopt_block_reports_the_projection_after_the_batch` because task 2.1
  depends on it being stated correctly.

## 2.1 — the tripwires, inverted

Their red run against the OLD expectation, after the carrier landed and before
the inversion — this is the red-first evidence the task asks for:

```
tests/test_due_state_emission_capture.py:281: in test_a_multi_write_command_carries_one_block
    assert "due_state" not in response, response
E   AssertionError: {'due_state': {'categories': {'prediction_window': 1}, …}
tests/test_due_state_emission_capture.py:339: in test_the_batch_scope_on_this_leaf_suppresses_nothing_today
    assert "due_state" not in scoped
E   AssertionError: assert 'due_state' not in {'due_state': {'categories': {'prediction_window': 1}, …}
2 failed, 13 passed in 4.63s
```

Both were **inverted, not deleted**, and only where they had to be:

- `test_a_multi_write_command_carries_one_block` now asserts one block on the
  response and an emission delta of one against a write delta of twelve.
- `test_the_batch_scope_on_this_leaf_suppresses_nothing_today` keeps its name
  because the name is still true. The leaf still reaches the per-write carrier
  `block_for_write` ZERO times — both its carrier-count assertions passed
  unchanged through this whole change — because the block it now carries is a
  BATCH block served once at the terminal. Its unscoped leg still carries
  nothing, and the docstring now says why: the change-only rule on a second
  invocation with unchanged totals, not the scope that was removed.

```
tests/test_due_state_emission_capture.py .. 15 passed in 4.16s
```

### The f23 flip, measured end to end against the installed CLI

`counter_emission_not_repeated_per_write`: **`unsupported` → decided.**

The bulk step was run exactly as `f23_dismissal.bulk_step` builds it —
`adopt legacy --mode copy-as-sources --selected-path …×12 --json` as a
subprocess against a real vault — and the emission ledger read before and after:

```
ledger before: {'writes': 0, 'emissions': 1, 'due_total': 1}
adopt rc: 0
adopt data keys: ['due_state', 'graph_sync', …, 'mutated', 'ok', 'paths', 'request_id', 'state', 'status', 'terminal', 'warnings_count']
CARRIES due_state in data: True
block: {"categories": {"prediction_window": 1}, "top": [{"category": "prediction_window", "due_since": "2026-08-25", "ref": "exomem://review/014840bc61fc4db7fe9259e3"}], "total": 1}
ledger after:  {'writes': 12, 'emissions': 2, 'due_total': 1}
DELTA writes: 12 emissions: 1
```

One block against twelve writes, on the real runtime, through the surface the
journey drives. Note `due_total` does not move: the block is delivered as the
first qualifying response of that CLI process's session — every CLI step is its
own process and therefore its own session — and NOT because the bulk pages
changed the counts. The f23 driver docstring now says exactly that; claiming the
twelve pages moved the counts would be the easy misreading and it would be
wrong. Note also that `_vault` is absent from the delivered block: the terminal
rebuilds it from validated fields only, so the server-internal path never
reaches the wire.

The driver's prose was updated in three places (module docstring's zero-carrier
inventory, episode step 5, and the `BULK_DOCUMENTS` comment). **No family,
assertion, predicate, gate, or OpKind changed**, proven rather than asserted by
comparing the parsed module against `HEAD` with docstrings stripped:

```
AST identical modulo docstrings/comments: True
```

**f26** (`hookless_episode_carrier`, withheld with amendment sequence 2): its
measured world changes too, and its before/after is recorded when sequence 2
activates. Nothing publishes meanwhile, and nothing in `benchmarks/` was
touched for it.

## 2.2 — response-contract check

**No recorded contract and no packaged digest moved.** No tool input schema
changed (no parameter added, removed, or retyped), and the five leaves' response
shapes are unchanged apart from the one advisory key the existing due-state
requirement already permits on a default compact mutating response
(`command-surface` §"Default compact mutating responses and recall responses MAY
carry one `due_state` block"). The two-phase rollout is therefore not engaged.

Checked rather than asserted — the packaged-surface and schema-fidelity guards
run green:

```
tests/test_tool_surface_contract.py tests/test_mcp_schema_fidelity.py
tests/test_due_state_*.py tests/test_mutation_terminal.py
211 passed, 1 warning in 34.54s
```

The one shape question this change had to answer is recorded under 1.6: the
`structured-files` receipt is a closed key set, and the carrier's commit gate is
what keeps it closed on the replay path that validates it.

## 2.3 — validation and scoped suites

```
npm exec --yes @fission-ai/openspec@1.10.0 -- validate --all --strict
Totals: 169 passed, 0 failed (169 items)
```

Scoped runs, all with
`PYTHONPATH=<worktree>/src EXOMEM_DISABLE_EMBEDDINGS=1` and a `/dev/shm`
basetemp:

| Scope | Result |
|---|---|
| the new carrier suite | `23 passed` |
| due-state ×5, mutation terminal, tool-surface contract, MCP schema fidelity | `211 passed` |
| the five leaves' own suites — adopt ×2, adoption studio ×3, structured files, client artifacts, media ×2, writer lease, REST registry | `522 passed` |
| adjacent surfaces — CLI core ops, governance egress, command-surface retry, hosted agent surface, epistemic loop primitives, media deletion/worker, attachment ingestion | `651 passed, 2 errors` |

The two errors are the known machine-global phantom, not this diff: both are
`_isolate_state_root` **teardown** assertions naming another worktree's pytest
as a concurrent candidate, and `tests/test_attachment_source_ingestion.py`
reruns solo at `59 passed`. Nothing in this change writes outside an injected
state root.

`uvx ruff check` is clean on every file this change touches. `writer_lease.py`
carries five findings (B009 ×2, BLE001 ×3) at lines 222, 226, 2101, 2138 and
2171 — all pre-existing, confirmed identical against `origin/main`'s copy of
the file, and none in the eleven lines this change added at ~4130.

**Not run here:** the full corpus. It belongs at the delivery boundary and must
be attributed against an `origin/main` baseline, because this repository has a
standing ~30 pre-existing `setup_wizard`/`demo`/`doctor` failures and one CLI
flag-parity failure that are not this change's.

## Files changed

| File | Change |
|---|---|
| `src/exomem/writer_lease.py` | `active_mutation_committed()` — the read side of the existing commit-boundary ContextVar |
| `src/exomem/due_state.py` | `block_for_batch()` — the `block_for_write` family's serve-only sibling, same disclosure boundary |
| `src/exomem/commands.py` | `_carrying_due_state()` and `_audit_fix_paths()`; the carrier applied at the nine enumerated invocations; batch deltas wired for `fix` and `backfill-ids` |
| `tests/test_due_state_bulk_carriers.py` | new — 24 pins covering 1.1–1.5 |
| `tests/test_due_state_emission_capture.py` | the two tripwires inverted, not deleted |
| `benchmarks/epistemic/journeys/f23_dismissal.py` | prose only (AST-proven); zero-carrier inventory retired |
