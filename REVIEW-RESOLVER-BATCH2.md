# Review: resolver batch 2 (`be34c8e7..feat/resolver-families`, 8 commits)

## Verdict: REQUEST_CHANGES

The domain and entity-type work is sound. The two Medium findings are in the
recovery watcher (close-memory-loop 5.10 / variants 1.3).

## Findings

### M1. A publication that becomes readable after 10 s strands the job it was meant to deliver (CONFIRMED mechanism)

- `epistemic_graph.py:4465` fires inside `_publish_available_marker_in_transaction`. In the full rebuild (`:3360`) that transaction writes to the *temporary* database. The swap into place happens later, after `wal_checkpoint(TRUNCATE)`, `integrity_check`, WAL sealing and ticketing, and the publish may still be refused.
- `server_runtime.py` `redrain_after_publish` polls `available()` for `VOCABULARY_REDRAIN_READY_SECONDS = 10`, then returns 0. Nothing signals again, so the queued job waits for an unrelated future publication. That is the stranding 1.3 set out to fix. The comment ("a publication that never commits is followed by another one") covers a withdrawn publish but not a late one.
- Reproduction (scratch test with the windows shrunk to 0.2–0.3 s through `__kwdefaults__`): enqueue a job, start `watch_vocabulary_recovery`, call `note_graph_published`, and make `available()`/`status` turn true 0.8 s later. Result: `AssertionError: job stranded after a late-readable publish`.
- Minimal fix: when the wait times out and `page(limit=1)` is still non-empty, call `published.set()` again so the watcher retries (it is still bounded by shutdown). A better fix signals after the swap or commit, not inside the transaction.

### M2. The watcher thread outlives `_stop_background_workers`, and nothing joins it (CONFIRMED)

- The thread started at `server_runtime.py:327-336` now lives until `_shutdown` is set. `_stop_background_workers` (`:290`) never stops it, and the lifespan joins only the activation thread.
- Reproduction: run `tests/test_derived_batch_receipts.py -k "pending_components_schedule_on_server_start or local_runtime_starts_and_stops_derived_drain"` with a session-end census plugin. Branch: `LEAKED_VOCAB_THREADS 2`. Base `be34c8e7`: `0`.
- `exomem-vocabulary-recovery` is missing from `tests/conftest.py:817` `_VAULT_WALKING_THREAD_NAMES`. The watcher can still reach a vault walk: `available()` → `_recall_projection_identity(disk_freshness=_disk_vault_freshness(...))` (`epistemic_graph.py:2240`, `:1548` `walk_vault_md`). My single probe did not hit that path (0 walks), but the fast path skips it only for an exact live checkpoint.
- Minimal fix: give the watcher its own stop event, set it in `_stop_background_workers`, and join it with a timeout (in the lifespan too). Add the thread name to the census set.

### L1. A kind-only reclassify of a source that already has a domain is now refused when the registry is malformed (CONFIRMED)

- `reclassify_source.py:452-456`: `effective_domain` falls back to `current_domain`, so `_projection` takes the strict path even though the caller did not supply a domain. Branch: `INVALID_DOMAIN_TAXONOMY`. Base: relocates to `Sources/Reports/Health/`.
- This goes beyond the ruling, which refuses only *captures that supply a domain*. `:423` also passes on `error.reason` without the registry path that `capture_refusal_reason` adds.
- Needs a ruling. If this should be allowed, the minimal fix is to use the lenient path when `domain is None`.

### L2. When a capture registers a new domain, it drops the strict guard (PLAUSIBLE, not reproduced)

- `add.py:538-542` omits `registry_guard` when `taxonomy_plan.writes` is non-empty. `plan_registrations` re-reads the registry (`source_taxonomy.py:859-864`) and guards against *that* read (S2), while the binding was resolved against S1.
- Scenario: between the two reads, someone edits the registry to add an alias equal to the new key. The append then produces a duplicate owner, and the strict registry fails closed for every later domain capture. The window is milliseconds, and the re-read already existed on base.
- Fix: refuse when the plan's source bytes hash differs from `binding.snapshot`.

## Hypotheses cleared

- **Critical section:** `note_graph_published` does a dict `get` and `Event.set`. It takes no lock beyond the Event's condition, does no I/O and cannot raise.
- **Rollback or withdrawal:** the watcher waits on `available()`, so a rolled-back or withdrawn publish never drains against unpublished state. It returns 0 once the bounded wait ends. (Wake-before-commit liveness is covered in M1.)
- **Double delivery:** `claim` is a CAS on `attempts`, and `complete` deletes only the row with `attempts+1`. If the process crashes after a claim, the row can be paged again, so delivery is at least once. Concurrent drainers never both complete one job. The review, activation and watcher paths share this code.
- **Spelling variants:** with an NFD `Café Research` folder present, the requests `café research`, `CAFÉ RESEARCH`, the NFD spelling, `cafe-research` and `Café_Research` all resolve to the existing folder. When two equivalent folders exist, the result is `AMBIGUOUS_DOMAIN_DESTINATION` with "reconcile them first … capture without `domain`".
- **Registry edited between resolve and commit (no registration):** the guard refuses the commit. Reverting `required_guards` turns `test_a_registry_change_after_resolution_refuses_the_commit` red.
- **No folder before commit:** restoring the `mkdir` turns `test_a_failed_capture_leaves_no_domain_directory` red.
- **Hosted disclosure:** the refusal names only the vault-relative `Knowledge Base/_Schema/source-taxonomy.yaml`. The absolute path appears only in the lenient loader's log line, which is unchanged from base.
- **Domainless captures under a malformed registry:** `op_capture_source` stores the capture, adoption's `_source_destination` returns `Sources/Articles`, and reclassifying a source without a domain succeeds.
- **Adoption matches capture:** both call `resolve_source_domain`, and `test_adoption_resolves_the_legacy_spelling_it_commits_into` covers it.
- **Evidence scopes:** no evidence code is touched, and `PUBLIC_FAMILIES` excludes it.
- **Entity receipts:** they carry the requested spelling, canonical id, parent folder and the 64-character registry fingerprint. Unknown types keep the registry refusal.
- **Test quality:** the touched modules pass (62 tests). Three reverts, each confirmed red: the registry guard, the `mkdir` removal, and `note_graph_published` → `pass`, which makes `test_a_job_stranded_by_a_withdrawn_graph_drains_after_the_next_publish` fail.

Command: `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider tests/test_source_vocabulary_resolution.py tests/test_vocabulary_family_receipts.py tests/test_vocabulary_recovery.py tests/test_vocabulary_authority_lifecycle.py tests/test_server_runtime.py`

## Recheck at `4b4596a3` (commits 41e4c54, d8dd8c7, 4b4596a)

I re-read every file this round touched: `add.py`, `epistemic_graph.py`, `reclassify_source.py`, `server_runtime.py`, `source_taxonomy.py`, `vocabulary_resolution.py`, `tests/conftest.py` and the three test modules.

- **M1: resolved.** `_publish_available_marker_in_transaction` no longer signals. `_note_graph_published` fires only for the live sidecar path, and only after the commit or swap:
  - the full-rebuild swap (`epistemic_graph.py:3209`);
  - the registry-rebind `replace_sidecar` (`:3670`);
  - `_publish_available_marker` (`:4430`), which `_mark_incremental_available` also uses;
  - the incremental `before_commit` (`:5688`);
  - the deferred drain (`:6012`).

  I found no swap site that skips the signal. A redrain that times out with work queued re-arms the watcher, up to `VOCABULARY_REDRAIN_RETRIES = 30` times in a row. My late-readable repro (0.3 s window, readable after 0.8 s) now **passes**: the job drains. A bound probe with retries set to 3 made 12 `available()` polls, then **0** more in the next 1.5 s.
- **M2: resolved.** The watcher has its own `_vocabulary_stop`, which `_stop_background_workers` sets. The thread is joined (5 s bound), and the lifespan joins it a second time after the activation thread. It is now listed in `_VAULT_WALKING_THREAD_NAMES`. Census on the same two `test_derived_batch_receipts` tests: `LEAKED_VOCAB_THREADS 0` (it was 2).
- **L1: resolved as ruled.** A kind-only reclassify of a `health` source under a malformed registry relocates to `Sources/Reports/Health/`. Supplying a domain still refuses with `INVALID_DOMAIN_TAXONOMY: domain taxonomy is malformed (Knowledge Base/_Schema/source-taxonomy.yaml)`, which is vault-relative. An ambiguous destination still refuses even without a supplied domain; the ruling only covered malformed registries.
- **L2: resolved as ruled.** `add.py:370-383` compares `registry_text_snapshot(plan.source_text)` with `binding.snapshot` and refuses on a mismatch with `STALE_VOCABULARY_BINDING` ("retry the capture", no path). A missing registry hashes the same way on both sides (`{"registry": "missing"}`), so first-time registration in a fresh vault is not falsely refused. A registry that is malformed at plan time hashes to `None`, which also refuses.

**Tests:** scoped `test_source_vocabulary_resolution`, `test_vocabulary_family_receipts`, `test_vocabulary_recovery`, `test_vocabulary_authority_lifecycle`, `test_server_runtime`, `test_derived_batch_receipts` and `test_epistemic_graph`: **164 passed**. I reverted each fix in a scratch copy, and every new test went red:
- re-arm: `…after_the_window_still_drains` failed;
- in-transaction signal: `…only_once_the_publish_is_readable` failed;
- join: `…joins_the_vocabulary_watcher` failed, and the conftest census errored in teardown;
- L1 fallback: failed;
- L2 check: failed.

**CI (PR #1438, head `4b4596a38c`):** 35 check runs: 25 success, 10 skipped (conditional lanes), 0 failed. The successes include `required CI gate`, `core tests` 12/12, `harness tests` 4/4, `lint + targeted types`, `OpenSpec validation`, `capabilities doc` and `product E2E`. The PR has no commit statuses and no Claude Approvals check run. `gh` is unavailable here, so I read the runs through the GitHub API.

**New, advisory (Low, not blocking):** the bounded re-arm allows up to 30 × 10 s of `available()` polling at 4 Hz per publication. That is 1,200 probes, each able to reach `_disk_vault_freshness` (a vault walk) when the checkpoint is not the exact live one. It was 40 probes before. Exponential backoff between re-arms would cut this without weakening the fix.

**Verdict: APPROVE**
