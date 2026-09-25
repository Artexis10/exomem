## Why

The Exomem Cloud P3 rehearsal (#1378, cross-lane defect 6) found a cell that, after a `read_only` to `running` recovery, answered `/health/ready` with `retrieval_unavailable` after its first governed write and never recovered. The gateway then refused that tenant with `503 CELL_NOT_READY`.

Leaving read-only mode is a pod rollout, so the defect is not specific to read-only mode. Any serving process that inherits a lexical catalogue published by an earlier process shows it on its first governed write. It reproduces with the embedding model absent and with embeddings cleanly disabled, where the write's full-index report is complete, so it is not an artifact of the stand-in image's missing model. The real-image rehearsal run hit the same 503.

The mechanism:

1. The inherited catalogue stores recall checkpoints stamped with the previous registry instance.
2. Warm-up admits retrieval, correctly, because admission compares projected state rather than lineage.
3. Start-up graph adoption makes the previous process's `vault` checkpoint a valid delta origin. Nothing does the same for `kb`.
4. The first governed write applies its catalogue rows, but it cannot bless `kb`: no retained history bridges a foreign origin nobody adopted.
5. The next health probe proves `kb` behind the live projection and revokes admission. That probe is deliberately side-effect free, and the write reported its rows applied, so no repair owner ever runs.

## What Changes

- **Warm-up rebase.** After warm-up proves an inherited catalogue current, it re-stamps any state-equal checkpoint from a foreign lineage with this process's live checkpoint. This happens under the publication barrier, and the rows are untouched. It is the same attestation a bounded write makes after applying its rows, so admission trusts nothing it did not already trust. The first write then blesses both scopes with an ordinary delta: it stays O(delta), needs no rebuild, and admission never drops. Only the serving repair owner does this. A standby still only proves.
- **Repair hand-off.** A bounded catalogue mutation (write upsert, watcher batch or delete) can apply its rows but leave a live scope unblessable. That happens when the stored checkpoint is missing, or when no retained history bridges it. In that case the mutation hands the scope to the managed repair owner once the barrier is released. Readiness may then honestly report the catalogue behind the live projection, but the repair publishes a current catalogue and readiness follows without a restart. An uncovered but bridgeable delta is unchanged: the watcher's own batch covers it shortly.
- The health probe stays side-effect free.

## Capabilities

### Modified Capabilities

- `instant-start`: inherited catalogue lineage keeps retrieval admitted across a worker replacement, and a bounded mutation that strands a scope hands it to repair.

## Impact

- Affected code:
  - `src/exomem/lexstore.py`: `LexicalStore.rebase_inherited_checkpoints`, `rebase_inherited_catalog_lineage`, the stranded-scope record in `_remember_live_witnesses`, and `_hand_off_stranded_scopes` on the three bounded-mutation entry points.
  - `src/exomem/warmup.py`: the managed proof calls the rebase.
- Affected tests: `tests/test_managed_retrieval_after_restart.py`.
- Cloud cells, the desktop managed service and promoted standbys all start through the same `warm_all`, so each gains the rebase.
