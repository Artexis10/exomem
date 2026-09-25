# U5E-REVIEW: fast durable-ack visibility and stranding (`fix/fast-ack-visibility` @ `e40971a7`)

Independent review. I did not write this code. I changed no product code. All probes were temporary, untracked files and have been removed.

- **Base:** `73e934e7` (origin/main 0.93.0).
- **Diff reviewed:** `git diff 73e934e7...e40971a7`, 14 commits: the visibility work (G1-G3), advisory parity, R1 plus `e8964ea7` and `e40971a7`, R2, R3, and R4 with B3.
- **Verified:** HEAD is `e40971a745c661de622a08b8556edb8202295ca1`.

## Verdict: **APPROVE WITH REQUIRED CHANGES**

The core safety claim holds under every interleaving I built. A handover cost a duplicate idempotent upsert, and I never saw a lane left holding non-current bytes with nothing owed to heal it. It holds because every receipt-owned fan-out (`converge_derived_component`, `converge_paths_from_current_bytes`) indexes the path's **current** bytes, not the receipt's recorded after-bytes. A fan-out whose admission snapshot moved underneath it takes `stale_report`, which re-queues the path through `deferred_index.add_full`. At FAST=0 the change is neutral against main. B3 is right. The R4 log lines are content-free. Advisory parity holds with 0 diffs on my own corpus.

Two things need changing before merge. Both are small.

1. **(Medium) Reconcile step 3b silently erases a live advisory result.** A `ready` advisory result whose target page is unchanged becomes `superseded`, so the user loses the warnings.
2. **(Medium) The "known gap" does not leave managed recall ready.** One hand delete of a just-created page, before its batch converges, turns every managed recall in the vault to `warming` until an operator runs reconcile. This happens even when both lanes already hold the page's absence. Either fix the code (R2 applied to a proven absence) or correct the stated claim.

The rest is Low or Info. F7 is a test gap that one of my mutants exposed.

---

## Findings

### F1 (Medium, required): `maintain --reconcile` step 3b turns a ready advisory result into `superseded` although its target is unchanged

- **Where:**
  - `src/exomem/derived_receipts.py:1288-1312`: `_supersede_batch` runs an unconditional `UPDATE write_advisory_results SET state = 'superseded' ... WHERE batch_id = ?`.
  - That function is called from `reconcile_stranded_batches` (`derived_receipts.py:2445`).
- **Why it is wrong:**
  - `deferred_write_advisory.resolve_result` (`:727-730`) already answers `superseded` whenever the target's fingerprint has moved.
  - A stored `superseded` state therefore only matters when the target has **not** moved. That is exactly the case R3 creates: a batch stranded by a *shared* page (a hand edit of `log.md`, `index.md` or a cited source) while its own page stayed in its after-state.
  - Before R3, whole-batch supersession needed newer custody over every path, the target included. The target had moved, so the two answers agreed.
- **Reproduction (ran it):**
  1. FAST=1. A default `remember` gives `advisory_sync=pending` plus a ref.
  2. Mark the result `ready`, which simulates a published sweep.
  3. Hand-append a line to `Knowledge Base/log.md` (no watcher).
  4. Drain until idle. The batch goes to `reconcile_required`.
  5. `resolve_result(ref)` gives `{'status': 'ready', 'advisories': []}`.
  6. Run `reconcile.reconcile(vault)`.
  7. The stored state is now `superseded`, and `resolve_result(ref)` gives `{'status': 'superseded'}`.
  8. Throughout, the stored `target_fingerprint` equals the current fingerprint of the page (`3fe7fb...` on both).
- **Effect:** with non-empty candidates, a near-duplicate or overlap warning the user was owed disappears behind `superseded`. It also applies to a still-`pending` result, whose sweep never ran: the user gets "superseded" instead of a result.
- **Smallest fix:** in the reconcile path, retire only the receipt custody and leave a finished advisory alone. For example, give `_supersede_batch` an `advisory: bool = True` flag and pass `False` from `reconcile_stranded_batches`. That route would instead run:

  ```sql
  UPDATE write_advisory_results SET state='superseded', updated_at=?
  WHERE batch_id=? AND state='pending'
  ```

  - `ready` and `failed` results are left alone.
  - A `pending` one is better set to `failed`/`advisory_unavailable` when the target fingerprint is unchanged.
  - Add a node to `tests/test_fast_ack_shared_pages.py` that pins it.
- **Spec:** the new requirement text says reconcile retires the batch "with its pending rows and advisory result". Narrow it to "and an advisory result that never ran". Also add a scenario for "a ready advisory survives reconcile".

### F2 (Medium, required): a created-then-hand-deleted page leaves managed recall `warming` vault-wide until reconcile, even though both lanes already hold its absence

- **Where:**
  - `derived_receipts.py:1211-1245` (`_delegated_paths`) and `_handed_on` (`:1174-1208`).
  - `_canonical_path_state` (`:920-927`) classifies a missing path with `before_hash is None` as `"before"`.
  - A `"before"` path is handed on only through `_newer_custody_wrote_bytes`, which looks for a newer tombstone. The R2 lanes test (`_recall_lanes_hold_current`, which already treats absence as proven absence) is never consulted for it.
- **Reproduction (ran it):**
  1. FAST=1 `remember`.
  2. `unlink` the new page before any drain pass.
  3. Drain until idle. The batch stays in `reconcile_required`.
  4. Doctor gives `fail`: `stranded batches 1; ... pending visibility warming(pending_visibility_unprovable)`.
  5. `pending_recall.overlay(vault).outcome == "warming"`. The lanes never held the page, so `recall_lanes_hold({page: None})` is true, but nothing asks it.
  6. After `reconcile`: `{'stranded': 1, 'retired': 1, 'remaining': 0}`, doctor `pass`, overlay `ready`.
  7. A second variant, where the page reached the lanes and was then deleted, also strands. Reconcile repairs it.
- **Against the packet's claim:** "the doctor flags it" is **confirmed**. "Managed recall stays ready" is **false**: every managed recall in the vault answers `warming` until an operator acts. The window is short (only before the batch converges), but when it hits there is no self-heal.
- **Smallest fix (recommended):**
  - In `_handed_on`, let a returned path whose recorded `before_hash is None` (current state: absent) also pass `_recall_lanes_hold_current(vault_root, rel)`.
  - Restrict this to batches already proven committed (`current.state in {'ready', 'completed'}`), so the torn-write concern at the first ack proof is unaffected.
  - That makes the deletion self-heal as soon as the watcher's removal fan-out, or the never-indexed state, shows absence in both lanes.
- **Minimum alternative:** keep the code and correct the design, packet and spec text to say this gap degrades managed recall until reconcile.

### F3 (Low): coverage queries scan every later receipt, inside the consistency guard, and receipts are never pruned

- **Where:** `_newer_custody_covers_path` (`derived_receipts.py:1092-1120`) and `_newer_custody_wrote_bytes` (`:1146-1171`).
- **The plan:** `SEARCH b USING INTEGER PRIMARY KEY (rowid>?)`, then two correlated PK lookups per row. `LIMIT 1` stops early only on a hit.
- **Measured** (synthetic store, 3 paths per batch, `log.md`/`index.md` in every batch; cost per (`covers` + `wrote_bytes`) pair on one path):

  | Later batches | `log.md` | Unique page |
  |---|---|---|
  | 10,000 | 12.6 ms | 12.9 ms |
  | 100,000 | 136.5 ms | 160.9 ms |

  - The shared page is not cheap either. `_newer_custody_wrote_bytes` misses in the ordinary case, so it scans everything.
  - `stranded_batch_count` + `custody_census` cost 49 ms at 100k (no index on `state`).
- **Why it matters:**
  - Every drain pass re-proves each stranded batch (`recover_prepared_batches`, which is bounded by `limit`). These scans run under `consistency_guard`, which excludes canonical writers.
  - A long-lived vault with a few stranded batches holding moved or returned paths therefore pays about 0.15 s of writer exclusion per path per pass.
  - That is linear in receipt history, and nothing caps the history.
- **Smallest fix:**
  - Store the batch sequence on `derived_batch_paths` (e.g. `batch_seq INTEGER`) and index `(rel_path, batch_seq)`, so both queries seek to `rel_path = ? AND batch_seq > ?`.
  - Or, with no schema change, run the path-driven plan (`derived_paths_lookup`) first when the path's history is short (probe `LIMIT K`), and fall back to the rowid plan otherwise.
  - Either way, adding an index on `derived_batches(state)` makes the census and stranded counts O(stranded).

### F4 (Low, contract text): the normative clause does not state the before-bytes rule, and it reads broader than the code

- **Where:** `openspec/changes/accelerate-durable-write-acknowledgement/specs/durable-write-acknowledgement/spec.md`, per-path clause (hunk at `@@ -37`).
- **The mismatch:**
  - The clause allows "a later state that a newer exact receipt covers ... or a later state whose current bytes both persistent recall lanes ... already hold".
  - The code is stricter for a path back at its before-bytes. Such a path is handed on **only** when a newer proven batch recorded exactly those bytes as its after-state. R2 is not applied, and plain coverage is not enough (`_handed_on`, `:1203-1207`).
  - My A→B→A probe shows the consequence. Batches `x→y`, `y→x`, `x→z`, then a hand revert to `x`:
    - Batch 1 is handed on, correctly.
    - Batch 3 stays `reconcile_required`, with its own unrelated page unindexed and the overlay warming, although the lanes hold `x`.
  - That strictness is a deliberate choice (the torn-write concern), and it is loud: doctor fails and reconcile heals. But the spec should say it.
- **The scenarios:**
  - Only the positive before-bytes scenario exists.
  - `test_before_bytes_no_newer_batch_wrote_stay_owed` has no matching scenario.
  - The new requirement has **2** scenarios, not the 5 the packet mentions. Four more were added under the existing receipt requirement.
- **Fix:** add a sentence to the normative clause, "a path back at its before-bytes is covered only when a newer proven receipt recorded exactly those bytes as its after-state". Add the negative scenario.

### F5 (Info): R2's docstring overstates what it checks

- **Where:** `_recall_lanes_hold_current` (`:1123-1143`) and the design text say R2 is "the overlay's own retirement test".
- **What differs:** the overlay's retirement (`pending_recall._settled_batches`) also requires `_components_allow_retirement`. R2 checks only lexstore plus memory_refs.
- **Effect:** a handed-on row stops shadowing vector and graph evidence before the batch's own embeddings and graph components have run. The components still run over that path later, from current bytes, so this is a short semantic-staleness window for a hand-edited page. It is not a correctness hole. The wording should say "the overlay's lane test".
- **Related G1 note:** `standalone_join_waived` lets the graph component complete on a started rebuild, so graph evidence for a fast-acked page can lag the lifted shadow. This is by design and specified in live-index-freshness.

### F6 (Info): the route-input advisory failure log carries a traceback over draft content

- **Where:** `deferred_write_advisory.py:396` logs `exc_info=True` while computing over the **draft title and body** held in `advisory_handoff`.
- **Assessment:** it copies the existing generic-path convention (`:448` here, `:391` on main), and the R4 ruling covers only acknowledgement and commit sites. Still, an exception raised inside `write_advisory_for` could carry draft text into the log. Consider logging `_content_free_cause`-style there too. The same applies to the new `drain_once` retirement warning (`derived_drain.py:291`), which is lower risk.

### F7 (Low, test gap): nothing pins that a coverer must be *newer*

- **Mutant M8** (`_newer_custody_covers_path`: `b.rowid > ?` changed to `b.rowid != ?`) survives all of `test_fast_ack_shared_pages`, `test_fast_ack_visibility`, `test_fast_ack_advisory_parity` and `test_derived_batch_receipts` (100 passed).
- With the mutant in place, a newer batch whose shared path moved (by hand) could hand it "on" to an *older* batch that also carried the path.
- Visibility would still converge only because fan-outs read current bytes. But it would silently retire exactly the rows R2 is meant to hold until the lanes catch up, which recreates the pre-R2 silent-handover risk.
- **Smallest fix:** add a node with an older completed batch carrying `P` and a newer batch whose `P` then moves by hand, with the lanes lacking it. Assert the newer batch stays `reconcile_required`. Mirror it for `_newer_custody_wrote_bytes`.

---

## What I verified clean (with evidence)

**Environment:**
- Model-free throughout. `EXOMEM_DISABLE_EMBEDDINGS=1`, except for the advisory differentials, which use a deterministic bag-of-words encoder as the parity suite does.
- `uv sync --extra embeddings` failed: the proxy refused `download-r2.pytorch.org` and `huggingface.co` (403).
- Pinned uv 0.11.28.

**Scoped suite:** 954 passed, 6 skipped (`sentence_transformers` absent, plus one Windows-only). Files run:
- `test_fast_ack_{advisory_parity,shared_pages,visibility}`
- `test_note_suggestions_knob`, `test_derived_batch_receipts`, `test_pending_recall_delta`, `test_deferred_write_advisory`
- `test_activation_manifest`, `test_writer_lease`, `test_fast_write_ack`, `test_fast_write_integration`
- `test_reconcile`, `test_doctor`, `test_doctor_write_path`
- `test_index_sync`, `test_deferred_index`, `test_corpus_aware`, `test_call_ledger`
- `test_relation_review`, `test_note`, `test_edit_operations`
- `test_semantic_write_latency_gate`, `test_write_advisory_suppression`, `test_relation_registry_graph_sync`

**Handover soundness (surface 1).** Each scenario was built with real receipts, the real drain, and lexstore checked against the on-disk sha256:

| Scenario | Result |
|---|---|
| The covering batch is later stranded: A `x→y`, B `y→z` proven; hand edit to `w`; drain | All batches `completed`, overlay `ready`, lexstore holds current bytes for every path. A's fan-out indexed `w` incidentally, because fan-outs read current bytes. |
| Two newer coverers, one unproven: A, B proven; C committed but unproven; A re-proven | A hands `index.md` to B. Then an in-process state wipe (`pending_recall.reset()`, standing in for a crash cut). Drain: A `completed`, B `superseded`, C `completed`, overlay `ready`, lanes current. |
| A→B→A chain plus a hand revert | No stale lane. The only non-converged batch is loud (F4). |

- **Why no interleaving leaves a stale lane with nothing pending:** a retirement needs a later change to exist. That change's owner is either a newer batch, which fans out current bytes, or a hand edit, which is the watcher's job exactly as without fast ack. A fan-out racing a write revalidates, then takes `stale_report`, then `add_full`.
- **The handover checks run under the guard.** `_prove_committed_guarded`, `complete_component` and `publish_pending_visibility` all call `_handed_on` inside `consistency_guard`, so the R2 check-then-retire is atomic with respect to governed writers (surface 3).
- **Coverage ignores in-flight rows.** Coverage requires the newer row state to be `live`/`retired`, so a coverer with only `prepared` rows does not count.

**ABA on before-bytes (surface 2):**
- A hand revert onto a newer batch's recorded after-bytes hands the older path on. The lanes still converge to the reverted bytes, because the coverer's fan-out reads current bytes.
- A hand revert with no newer after-match stays owed (the author's node plus my chain).

**Rowid ranging (surface 6b):**
- `derived_batches` is a rowid table, inserted with plain `INSERT` only.
- No `DELETE`, `REPLACE` or `VACUUM` exists anywhere under `src/`.
- Writers serialize under the lease, so rowid order is commit order and no rowid is recycled. Cost: see F3.

**Reconcile 3b (surface 4):**
- Idempotent: a second run gives `{'stranded': 0, 'retired': 0, 'remaining': 0}`.
- Works as the service routes it, as `commands.op_reconcile` inside `LeaseManager.invoke`: terminal `committed`, stranded 0, no deadlock on the nested `consistency_guard`.
- Retirement re-checks both the state and the lanes under the guard, in `BEGIN IMMEDIATE`. The converge fan-out runs outside the guard, the same as existing reconcile steps.
- Bounded by `STRANDED_RECONCILE_LIMIT=256`.
- Advisory handling: see F1.

**B3 and `commit_point=False` (surface 5).** Parametrized over FAST in {1, 0}:

| Cut | Outcome | Retry |
|---|---|---|
| Guard race after the manifest install | `STALE_SEMANTIC_WRITE` (not uncertain), manifest present, leaf unchanged | `committed`, manifest byte-identical |
| Crash inside the manifest install (`activation_manifest.batch_atomic_write` raises) | Plain error (not uncertain), manifest absent, no leftover temporaries, leaf unchanged | `committed` |

- The manifest census comes from the **before** corpus (`preflight.activation_census`, `preliminary.before_corpus.activation_census`), so a manifest left behind by an aborted leaf is consistent with a vault that lacks the leaf's write.
- I found no leaf where the manifest install was the user-visible commit.
- `init` and `reconcile` keep the default, correctly: there the install is the operation.

**FAST=0 unchanged (surface 6a).** Head vs a `73e934e7` worktree, same probe, 3 runs each, 8 ops (remember ×4 including `suggestions=true`, a body edit, a tags-only edit, a surgical edit, capture):
- Normalized terminals and warnings are identical on every op in every run.
- `derived_batches` is empty on both trees.
- Doctor differs only by the new `fast_ack_custody` check (`pass`, "fast ack inactive").
- One field, `vocabulary_sync` on the `suggestions=true` op, flipped between `current` and `unavailable` across runs **on main too**. That is pre-existing nondeterminism, not this branch.
- Latency/CPU figures: not reproduced. There is no service here.

**Advisory parity (surface 8).** My own corpus is independent of the author's: a near-duplicate of two pages, another type's twin, a two-page overlap, a body edit, a tags-only edit, a surgical edit and capture. Flag off inline vs flag on deferred through `advisory_result_ref`:
- **0 diffs.** 12 warnings over 5 swept ops (2/2, 0/0, 4/4, 3/3, 3/3), same text and order.
- A tags-only edit gives `advisory_sync=not_required`.
- Capture keeps its inline sweep (3 = 3) with `not_required`.
- `suggestions=true` stays synchronous.
- `note_type` is validated upstream, so `types_filter=[note_type] if note_type else None` cannot diverge from main's `[note_type]`.

**Content-free logging (surface 7):**
- A post-commit leaf raising `Leaky(code="Knowledge Base/.../secret-title.md", msg with path and body)` logs exactly `(class=Leaky stage=post_commit_leaf)` at both FAST values.
- The `_content_free_cause` regexes drop anything that is not a closed token.
- The doctor line and details carry counts, one age and closed codes only.
- `readonly_visibility` and `snapshot_pending_visibility` use `_connect_receipt_read` only: no writes and no retirement.
- See F6 for the two inherited `exc_info` sites.

**Known gap (surface 9):** the doctor flags it (`fail`, with the reconcile remediation). Managed recall does **not** stay ready (F2).

**Contract (surface 10):**
- The per-path clause matches the ack proof, claim re-proof, completion and publication sites, apart from F4.
- The R2 clause names the right lanes: lexstore plus memory_refs, per `_lanes_hold`.
- The new requirement matches the code, apart from F1's advisory wording. It has 2 scenarios.
- Tasks 5.13-5.16 are ticked, and their text matches the code, test names and log fields. 5.11 is correctly left open.
- I could not see the author's mutation matrix; my own mutation run is below.

**Mutation spot-check (mine):** 9 mutants against the fast-ack suites plus `test_derived_batch_receipts`. Each mutant was applied to the working tree and reverted with `git checkout` straight after its run:

| Mutant | Result |
|---|---|
| M1: completion back to whole-batch `after` | killed (6) |
| M2: R2 lanes clause always false | killed (2) |
| M3: before-bytes always handed on | killed (2) |
| M4: coverage accepts `prepared` rows | killed (1) |
| M5: reconcile skips the lane check | killed (1) |
| M6: publication owns every path | killed (1) |
| M7: `_commit_existing` manifest `commit_point=True` | killed (1) |
| **M8: coverage `b.rowid > ?` changed to `b.rowid != ?` (an older batch may "cover")** | **SURVIVED**: see F7 |
| M9: deferred advisory ignores route inputs | killed (1) |

**Gates.** The only file added is this review. Each gate was run on the tree with it in place:

| Gate | Result |
|---|---|
| `validate-public-artifacts.py --repository` | clean (4453 files) |
| `ruff check --select F src tests` | all passed |
| `generate-capabilities.py --check` | current |
| `openspec validate --all --strict` | 212 passed, 0 failed |

**Not run:**
- Anything needing torch or a real encoder, because the network blocked the downloads.
- The author's live burst and latency gates, because there is no running service here.
