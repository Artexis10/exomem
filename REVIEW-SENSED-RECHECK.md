# Security recheck: sensed epistemic model, slice 1 correction round

Reviewed PR #1437 (`feat/sensed-epistemic-model` at `cb77d60`), commits `5ed401df..cb77d60`. The reviewed branch was not modified.

## Verdicts

| Item | Verdict | Basis |
|---|---|---|
| H1 cap decided by withheld pages | **FIXED** | `_open_view` (`sensed_model.py:1555`) gates on `band_audience_allowed`. Under a non-empty policy only an owner-bound principal gets sensed items, and an undecidable audience gets none. I rebuilt the twin independently: hub 12 units, later 10 units, a withheld 1-unit page, 132 candidates, hub capped. Restricted, research-purpose and unresolved principals get `null` on both pages in both vaults, and `for_packet` output is byte-identical. The owner is served. Task 8.3 is filed before slice 5. |
| M2 stale status served as current | **PARTIAL** | Edges whose page signature or instrument key is not live are dropped (`:1381`), and a read of another snapshot carries no status (`:1586`). But when every edge is dropped, `_status` returns None (`:1529`), so no `evidence_complete: false` is reported, which the ruling and task 8.2 require. My probe (edit before any tick) got None. |
| M3 write-burst probe and tasks | **FIXED** | The probe is committed, 8.4 and 8.5 are filed, and the numbers are in `design.md`. |
| L4 REPLACE bypass | **FIXED** | `BEFORE INSERT` triggers (`sensing_ledger.py:68-87`). |
| L5 child env | **FIXED** | An allowlist (`sensor_worker.py:268-304`). Names containing KEY, TOKEN or SECRET are denied, so `EXOMEM_INTERNAL_INGRESS_KEY` and `HF_TOKEN` are stripped. `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` are forced. The argv carries no secrets, and the state-root variables are kept. |
| L6 sensing-off config read | **FIXED** | The env var is read first, then a 5 s memo (`sensing.py:65-89`). |
| L7 0600/0700 | **FIXED** | `sensing/` is chmodded to 0700 on every connect. The ledger, projection and state JSON are created 0600. |
| L8 θ per encoder, rounded | **PARTIAL** | The predicate compares the rounded cosine (`:644`). θ is keyed by the `model\|pooling\|l2` prefix (`:393-400`), not the exact fingerprint, so a quantised build or a change of prefix or `max_seq` reuses 0.72. It fails closed for unknown families. |
| L9 ledger genesis | **FIXED** | Written once and protected by triggers. `_follow_ledger` reprojects when it changes. |
| INFO | **Recorded** | Under "Open items" in `design.md`. |

**The L4 deviation is accepted.** `append` assigns `MAX(seq)+1` inside `BEGIN IMMEDIATE`, so a legitimate seq collision cannot happen. `ABORT` is louder than the ruling's `IGNORE`, and REPLACE still cannot rewrite a row. A duplicate id under `INSERT OR IGNORE` still counts zero changes.

## Attack pass

- **Withheld = absent.** In `edges()`, `visible()` is checked before the stale-drop counter, so a withheld partner cannot flip `evidence_complete`. `complete()`, `_component` and `_chain` all filter on `visible()`. The packet keeps four fields, and the budget is charged only for a served line. `capped` and `candidates` are never served. The twin held on all three carriers.
- **Replay.** I deleted the projection and re-settled from the same ledger. The status and edges came back identical. The cosine selection depends only on the current count (`_follow_cosine`).
- **Sensing-off cost.** Each read costs an env lookup, a lock and `monotonic()`, plus one config read per 5 s. Nothing opens the policy, projection or ledger before `enabled()`.

## New concerns

1. **LOW: `sensed_model.py:1484`.** `_chain` takes supersession partners from the projection without `live()`. A partner deleted or re-dated since the last tick stays in `chain` until the next tick.
2. **LOW: `sensed_model.py:1529`.** This is the M2 residual. When `_dropped[path] > 0` and nothing survives, return `{"evidence_complete": false}` with zero counts. The drop count only counts visible partners, so this leaks nothing. Otherwise, correct task 8.2 and D7.
3. **INFO: `sensing_ledger.py:38`.** Only the `genesis` meta row is protected. A forged `schema_version` makes `connect` refuse the ledger, which fails closed.
4. **INFO: `sensing.py:66`.** The config memo is process-global, not keyed by `EXOMEM_CONFIG_PATH`.
5. **INFO.** Ungoverned tombstoned pages are withheld for everyone but can count toward the cap until they are purged. It is the same for every caller, so it is no audience channel. It belongs with task 8.3.

## Tests and CI

- Scoped run at `cb77d60`, pinned uv, `CUDA_VISIBLE_DEVICES=` and a temporary `XDG_STATE_HOME`, over `tests/test_sens*.py tests/test_dreamer*.py tests/test_upkeep*.py` plus the status-egress tests (`test_governance_egress.py`, `test_derived_identifier_egress.py`): **757 passed, 4 skipped** in 12m26s. The skips are the real-weights pin test.
- Independent scratch probes (the twin on every carrier, replay, all edges stale): 3 passed. Not committed.
- PR CI on `cb77d60`: 37 check runs, all `success` or path-filtered `skipped`, and `required CI gate` passed.

## Decision

**APPROVE.** H1 is closed on every carrier, and nothing stale is served as current. The M2 and L8 residuals and concern 1 are low-severity reporting and calibration gaps, not disclosures. They can land here or before slice 2, at the owner's discretion.
