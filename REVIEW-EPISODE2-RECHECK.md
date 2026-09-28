# Security recheck: episode slice 2 correction round (PR #1432)

**Verdict: REQUEST_CHANGES.** One withheld-versus-deleted oracle remains on the page-leaf resume path. The fix is one line plus one test.

Reviewed `origin/feat/episode-slice-2` @ 47331d2, the commits after 066a22b. This recheck is close-memory-loop 5.5, so the 5.5 tick should wait for N1.

Runs (umask 022, `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider`):
- `tests/test_episode_{records_leaf,coverage,destinations}.py`: 43 passed.
- `tests/test_curation*.py tests/test_episode_workflow.py`: 111 passed.

## Original findings

| ID | Status | Evidence |
|----|--------|----------|
| M1 | **PARTIAL** | Records leaves: `_refused_for_caller` (`episode_workflow.py:704`) resolves the collection and checks mutation visibility. A deleted collection raises there too, so withheld and deleted take the same branch: `stale`/`CURATION_BINDING_STALE`, pending, attempts=0. Records postcommit matches through the receipt readback. Page postcommit is fixed by `_tip_is_live`'s `keep` (`episode_reconciliation.py:171`). Uncertain leaves stay uncertain in both cases (`:687`). The page-leaf pre-attempt path still leaks: N1. |
| L1 | **FIXED** (docs) | `design.md` and the `curation.py` comment state the ruling. Bypass check: `maintain_memory` curation apply of an episode-sealed run reaches `records.append_record`, which calls `require_mutation_visibility` (`records.py:304`). A withheld collection is refused there as `COLLECTION_NOT_FOUND`. |
| I1 | **FIXED** | `_folded_segment` applies NFKC, casefold and strip; `allow_records` requires `parts[0] == "Records"`. Probes are under (c). |
| I2 | **FIXED** (as scoped) | Every leaf runs `prepare_proposal` before any `seal_proposal`. A Low residual is under (d). |

## NEW CONCERN N1 (Medium): a registry change separates withheld from deleted

**Where:** `src/exomem/episode_workflow.py:772-776`.

**Cause:** A refused leaf returns `CURATION_BINDING_STALE` before `_blockers` runs. A deleted page goes through `curation.preview`, where `_binding_blockers` puts `CURATION_REGISTRY_CHANGED` rows before path rows (`curation.py:1548-1556`). Entity-candidate errors are also inserted at index 0 (`:1630`).

**Scenario:**
1. A restricted caller prepares and dispositions a page leaf.
2. The owner withholds the page. Separately, the owner adds `_Schema/contracts/*.yaml` or edits the entity or relation registry, which is routine.
3. On resume, the withheld page gives `CURATION_BINDING_STALE`. A deleted page, or a live unchanged one, gives `CURATION_REGISTRY_CHANGED`.

The withheld code is unique, so it is a one-bit "exists but withheld" oracle.

**Reproduction:** Run `test_a_relation_source_withheld_before_its_attempt_resumes_like_a_deleted_one` with this added after the unlink:

```python
contracts = vault / "Knowledge Base" / "_Schema" / "contracts"
contracts.mkdir(parents=True, exist_ok=True)
(contracts / "extra.yaml").write_text("x: 1\n", encoding="utf-8")
```

Its byte-identity assertion fails:

```
- "CURATION_REGISTRY_CHANGED"}], "status": "stale"}
+ "CURATION_BINDING_STALE"}], "status": "stale"}
```

**Minimal fix:** Stop `refused` from short-circuiting the preview:

```python
blockers = _blockers(session.vault_root, binding["run_id"])
if refused or blockers:
    return "stale", {"leaf_id": leaf_id, "code": (blockers or ["CURATION_BINDING_STALE"])[0]}
```

I applied this locally only (not pushed). With it, the reproduction and every withheld/deleted test in the coverage and Records-leaf modules pass (8 passed).

A cleaner option is to pass the caller's `keep` into `_binding_blockers`, so a withheld path reads as absent. Either way, add the reproduction as a regression test.

## Attacks

**(a) Byte-identity**
- Records: identical at prepare, disposition, resume (unattempted, uncertain, committed elsewhere) and postcommit.
- Pages: identical except N1.
- Timing (informational): the withheld path skips the preview/reconcile I/O that the deleted path runs (`:687`, `:772`). The gap is local and small. The N1 fix evens out the pre-attempt path.

**(b) Principal and replay**
- Journals are keyed by owner audience (`episode_store.py:140-202`), so another principal cannot open, resume or digest-match the episode.
- The check reads `effective_principal()` at resume time.
- `run_id` comes from the journal, never the caller.
- Replaying a run through curation apply meets the writer's own gates (L1). The episode then sees `_committed` and reports `diverged`/`EPISODE_OUTCOME_UNCERTAIN` for a withheld target.
- No bypass found.

**(c) Unicode paths**
- Refused as protected: full-width `Ｒｅｃｏｒｄｓ`, `Records` followed by U+3000, U+00A0, U+FF0E or U+2024, `Workflow_Contract.`, and `workflow-contract` followed by U+3000.
- Under `allow_records`: `records/…` and `Records./…` are refused; `..` gives `INVALID_CURATION_PATH`.
- Admitted: `Re` + U+0301 + `cords` and `Rec` + U+200B + `ords`. These are distinct names on ext4, NTFS and APFS, so this is correct. HFS+ drops some ignorable code points (U+200C/D), a legacy edge only.
- Deeper Records segments (`Records/c./x`, `Records/Ｃ/x`) pass the path gate. Harmless: execution uses the sealed `manifest_path` and item key, and the witness must match the Records receipt.

**(d) Prepare-then-seal TOCTOU**
- No new authority window. Bindings freeze hashes, so later changes are stale at execution, and authorization is re-checked per caller at resume.
- Residual (Low): orphans remain possible if `seal_proposal` fails for leaf k>1 (for example, `create_forward` re-runs `_require_plan_relocation_history`), or if `session.transitions` raises `EPISODE_REVISION_CONFLICT` after sealing. Such plans are inert and their run ids are never returned. A follow-up could sweep unbound runs; not blocking.
