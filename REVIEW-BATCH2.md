# Review: PR batch 2 (#1373, #1374, #1375, #1376, #1379)

This is an independent review; the reviewer wrote none of these PRs. Each PR head was checked out detached in a throwaway worktree:

| PR | Head |
|---|---|
| #1373 | `e51ff6f` |
| #1374 | `df63cd1` |
| #1375 | `ea04296` |
| #1376 | `6fa04c0` |
| #1379 | `43798c6` (base `fix/fast-ack-visibility` @ `e40971a`) |

Method:
- Tests were run with the pinned uv 0.11.28 and `CUDA_VISIBLE_DEVICES=`.
- To show each PR's tests fail first, only the production `src/` change was reverted to the base, the tests were rerun, and then the change was restored.
- Probes were throwaway scripts outside the repository and temporary state roots or HOMEs. Nothing but this file is committed.

Several local failures are environmental, not caused by any of these PRs:
- `HOSTED_RUNTIME_ID_INVALID`, because the sandbox runs as root.
- wheel-staging `uv exit 2`.
- occasional state-root teardown guards tripped by the parallel worktrees.

Each of those was confirmed to fail identically on the base.

## Summary

| PR | Verdict | Blocking items |
|---|---|---|
| #1373 | **REQUEST_CHANGES** | The "distinctive term" threshold scales with the number of collections, so the bug returns in any vault with more than about 4 collections. The title check misses multi-word, hyphenated and accented titles. |
| #1374 | **REQUEST_CHANGES** | CI is red (unpinned subprocess encoding). The refresh overwrites user-modified hooks with no consent or backup, and wires hook events the profile never had. |
| #1375 | **APPROVE WITH REQUIRED CHANGES** | The `manual_first` rewording changes meaning. Two justification comments are wrong. |
| #1376 | **APPROVE WITH REQUIRED CHANGES** | `setup --remote` still writes its secrets `.env` inside the vault named in the same run. |
| #1379 | **APPROVE WITH REQUIRED CHANGES** | F1 is fixed. The F2 heal is lost when a batch is proven but not published before recovery re-proves it. |

---

## #1373 — fix(routing): suggest a Records collection only on a subject match

**Verdict: REQUEST_CHANGES**

### Findings

**1. Major — the fix stops working as the collection count grows.**

*Location:* `src/exomem/collection_claims.py:96-101`, `ceiling = (total_targets + 1) // 2`.

A matched term counts as "distinctive" whenever at most half of the targets declare it. In a real vault with many Records collections, generic terms (capture, release, review) are almost never declared by more than half of them. So the false positive this PR targets comes straight back.

For an odd N the ceiling also treats a bare majority as distinctive: 3 of 5 passes. That contradicts the docstring ("not shared by a majority").

*Reproduction:* a probe calling `route()` from main and from the PR on the same targets.

```
F0 PR's own bug corpus (4 targets), terms capture/release/review   main=Metrics  PR=None
F1 same + 2 unrelated collections (6 targets)                      main=Metrics  PR=Metrics
F1b same + 6 unrelated collections (10 targets)                    main=Metrics  PR=Metrics
F5 5 targets, every matched term shared by 3/5                     main=W        PR=W
```

With 6 targets the ceiling is 3, and "capture" (declared by 3 collections) passes.

*Smallest fix:*
- Make distinctiveness independent of N. For example, require at least one matched term that the runner-up does not declare, or that only the winner declares (`frequency[term] == 1`).
- If the majority rule stays, use `frequency[term] * 2 <= total_targets`.
- Add a regression node: the PR's corpus plus 2 unrelated collections.

**2. Major — false negatives: the title check only works for single ASCII words.**

*Location:* `src/exomem/collection_claims.py:120`, `normalize_text(target.title) in normalize_terms(raw_terms)`.

This compares the whole normalized title against single tokens. It can only ever match a title that is one plain word of 3 or more characters. Genuine subject matches that route on main are now silent. In every corpus below, each matched term is shared by 3 of 4 collections:

```
F2  title 'Blood Pressure', terms blood/pressure/reading          main=BP        PR=None
F2b raw phrase term 'blood pressure reading'                      main=BP        PR=None
F2c title 'Blood-Pressure'                                        main=BP        PR=None
F3  title 'Workouts', terms singular 'workout'                    main=Workouts  PR=None
F4  title 'Café', terms include 'café'                            main=Cafe      PR=None
F4b title 'Cafe' (ascii)                                          main=Cafe      PR=Cafe   (control)
```

*Smallest fix:* `title_terms = normalize_terms([target.title]); if title_terms and title_terms <= normalized: return True`. This covers the multi-word, hyphen and phrase cases.

The accent case comes from the existing tokenizer (`[^a-z0-9]+`). That predates this PR, but this PR now makes routing depend on it. Plural folding is optional. Add nodes for F2 and F2c.

**3. Minor — this is a contract change, not a no-op.**

`route()` also drives:
- `claims_match` in `working_set_resolve.py:815`;
- current-state collection selection in `working_set_state.py:181`.

`openspec/specs/context-activation/spec.md:88-95` says claims coverage of the turn's terms yields `claims_match`. The F0 and F2 shapes no longer do. The PR body says "no contract change" and adds no OpenSpec delta.

*Fix:* add the requirement delta, or state the narrowed contract explicitly.

**4. Minor, speculative (arithmetic only) — the result depends on which targets a caller passes.**

`due_state.routing_targets()` filters targets per principal, and the working-set callers build their own sets. Because the threshold depends on N (finding 1), the same observation can route for one caller and not for another. No end-to-end flip was reproduced.

### Verified clean
- `pytest tests/test_collection_claims.py tests/test_working_set_resolve.py`: 239 passed.
- Red-first: with only `collection_claims.py` reverted, `test_route_stays_silent_on_generic_term_overlap_alone` fails (1 failed, 60 passed). The guard node passes on main as well, as expected.
- 17 routing-adjacent modules: 823 passed.
  - The 8 errors were in `test_unreflected_outcomes.py` during the combined run only. Run alone, it gives 38 passed, so this is an isolation issue, not this PR.
- `uvx ruff check --select F` on the touched files: clean.

---

## #1374 — feat(upgrade): refresh installed Claude Code hooks after a managed upgrade

**Verdict: REQUEST_CHANGES**

### CI failure (diagnosed from the job log first)

- Run `36172023999`, job `core tests (py3.13, shard 5/12)`. The `required CI gate` is red because of it; every other job is green or skipped.
- The failing test:

```
FAILED tests/test_subprocess_text_encoding.py::test_every_text_mode_subprocess_pins_its_encoding
AssertionError: these calls decode a child's output with the host's active code page;
add encoding="utf-8" (and an explicit errors= policy)
assert ['install_hook.py:1557'] == []
```

### Findings

**F0 — Blocker (CI) — the new subprocess call does not pin its encoding.**

*Location:* `src/exomem/install_hook.py:1557`. The `subprocess.run([... "install-hook" ...], capture_output=True, text=True, ...)` call in `refresh_wired_profiles` has no `encoding=`.

*Reproduction:* the same test fails locally (1 failed, 6 passed). A throwaway edit adding `encoding="utf-8", errors="replace"` (the policy the test names) gave 13 passed across the encoding tests and `test_hook_refresh_after_upgrade.py`. The edit was then reverted.

*Fix:* add those two keyword arguments.

**F1 — High — overwrites a user-modified hook without consent and keeps no backup.**

*Location:*
- `install_hook.py:1512-1594` now runs the full `install-hook` unattended after every upgrade.
- `_deploy_file` (`install_hook.py:1065`) always atomically replaces the script.

There is no modification detection: no content hash, no install manifest, no mtime check.

*Reproduction* (temp HOME):
1. Install, then replace `hooks/exomem-retrieve-nudge.sh` with a custom script and set it to mode 0500.
2. Run the refresh. Output: `success: True | custom survives: False | backup files: []`.
3. Separately: a user-set `timeout: 45` on the exomem settings entry came back as `10`.

*Smallest fix:*
- Record the sha256 of each deployed file at install time.
- On refresh, replace a file only if its current hash matches a version exomem shipped.
- Otherwise skip it and report it as `modified`, so doctor can warn.
- Preserve user-set fields on existing settings entries.

**F2 — High — installs hook events the profile never wired.**

*Location:* `install_hook.py:1477-1494` (`discover_wired_profiles`) plus the unconditional full `install-hook` at `:1555`.

A profile only needs the retrieve nudge to qualify. The refresh then wires every event. This contradicts the function's own docstring ("never one that never asked for them") and the new docs.

*Reproduction:* a profile wired only for `UserPromptSubmit`:

```
before: ['UserPromptSubmit']
after:  ['PreCompact','SessionEnd','SessionStart','Stop','UserPromptSubmit']
```

This added `Stop: bash ~/.claude/hooks/exomem-capture-nudge.sh`. A user who deliberately removed the blocking Stop nudge gets it back on every upgrade.

*Fix:* refresh only the scripts and settings entries already present. For example, pass the already-wired events as the spec set, and skip continuation hooks that are not wired.

**F3 — Medium–High — rewires profiles to a hook directory it guesses.**

*Location:* `install_hook.py:1493`, `hook_dir = profile_dir / "hooks"`. The directory is guessed rather than taken from the command actually wired, and settings paths are not deduplicated after resolution.

*Reproductions:*
- **Custom hook dir.** `bash "/…/custom-hooks/exomem-retrieve-nudge.sh"` was rewritten to `~/.claude/hooks/…`, silently dropping the user's own script location.
- **A `~/.claude-work` profile that uses `~/.claude/hooks`.** It was rewritten to an absolute `~/.claude-work/hooks/…` path, and a second hooks directory was created.
- **`~/.claude/settings.json` and `~/.claude-work/settings.json` both symlinked to one dotfile.**
  - The file was refreshed twice.
  - It ended with a machine-specific absolute path, and the default profile now runs the work profile's hooks.
  - Each upgrade leaves two `settings.json.backup-*` files next to the dotfile.

*Fix:*
- Deduplicate on `settings_path.resolve()`.
- Take `--hook-dir` from the directory in the existing wired command, with `~` expanded.

**F4 — Medium — "never raises" is not true, and one bad profile stops all of them.**

*Location:* `install_hook.py:1490-1491`. `settings_path.exists()` raises `PermissionError` on Python 3.11+.

*Reproduction:* `~/.claude-locked` set to mode 000, run as a non-root user via `setpriv`.
- Direct call: `PermissionError [Errno 13] … .claude-locked/settings.json`.
- Wrapper: `success: False, profiles: []`, with nothing persisted for doctor ("no managed upgrade has refreshed hooks yet": pass).
- The healthy `~/.claude` profile was never refreshed.
- The upgrade itself still exited 0.

*Fix:*
- Wrap each profile's discovery in `try/except OSError` and record a failed entry.
- Persist the report on the wrapper's error path too.

**F5 — Low — some wired profiles are never found, and doctor reports a pass.**

`CLAUDE_CONFIG_DIR` and `EXOMEM_HOOK_HOME` are not candidates. A probe with `CLAUDE_CONFIG_DIR` pointing at a wired directory returned `profiles: []` with `success: True`. A wired profile with malformed `settings.json` is also skipped silently (`profiles: []`), and doctor passes that too.

*Fix:* add both variables to the candidates, and report unreadable settings as a failure.

**F6 — Low — creates `~/.claude` on hosts that never used Claude Code.**

`_write_upgrade_refresh_report` (`install_hook.py:1501-1509`) runs `mkdir(parents=True)`. Probe with a home that has no Claude directory: afterwards `~/.claude/.cache/exomem-nudge/upgrade-refresh.json` exists.

*Fix:* write the report under the exomem state root, or skip it when no profile was found.

**F7 — Low — silences the capture nudge in live sessions.**

The refresh goes through `install_hook`, which calls `_mark_restart_pending`. The marker was present after the refresh. It keeps the Stop nudge quiet for up to 24 hours, although the MCP server never restarted.

*Fix:* do not write the marker on an upgrade refresh.

**F8 — Low, code reading only — `--resume` never refreshes hooks.** The `--resume` roll-forward path (`service_upgrade.py`, around line 490) never calls the refresh.

### Verified clean
- **The upgrade itself is fail-soft.** `_refresh_hooks_after_promotion` catches everything. A bad interpreter, a timeout and a read-only hooks directory were each reported per profile and never raised.
- **No partial writes.** Writes go through a temp file and an atomic replace. The originals were intact after the failure probes.
- **Symlinked hook files are refused** ("refusing unsafe hook destination"), and the symlink survives.
- **Unwired profiles are left untouched.**
- **Red-first holds.** With `src/` reverted, all 6 tests in `test_hook_refresh_after_upgrade.py`, the new upgrade-result node and the 4 new doctor nodes fail. They pass on the head, except F0.
- `uvx ruff check --select F` on the touched files: clean.

---

## #1375 — perf(bootstrap): restore compact bootstrap headroom

**Verdict: APPROVE WITH REQUIRED CHANGES**

Every instruction the bootstrap spec requires is still present. The byte numbers in the commit message are exact. The required changes are to wording and comments only, and they fit in the recovered headroom.

### Measured numbers

- Bytes are `len(json.dumps(payload))`, the repo's own budget metric.
- Tokens use the repo's only approximation, characters / 4.
- The ceiling is 63,300.

| Surface | Level | main bytes (~tok) | main headroom | PR bytes (~tok) | PR headroom | Floor |
|---|---|---|---|---|---|---|
| default | unset | 62,857 (15,714) | 443 | 62,640 (15,660) | 660 | 512 |
| claude-code | unset | 62,866 | 434 | 62,649 | 651 | 512 |
| default | balanced | 62,853 | 447 | 62,636 | 664 | 512 |
| claude-code | balanced | 62,862 | 438 | 62,645 | 655 | 512 |
| default | maximal | 63,218 (15,804) | 82 | 63,001 (15,750) | 299 | 256 |
| claude-code | maximal | 63,227 (15,806) | 73 | 63,010 (15,752) | 290 | 256 |
| default | off / light | 60,655 / 60,939 | 2,645 / 2,361 | 60,438 / 60,722 | 2,862 / 2,578 | – |

- **Full / diagnostics:** 217 bytes smaller at every level (for example, full goes from 116,822 to 116,605).
- **Session payload:** 115 bytes smaller (21,739 to 21,624).
- **The commit claims** of 217 bytes saved (115 + 48 + 54), 664/655 at balanced and 299/290 at maximal all match.

### Findings

**1. Medium (required) — `manual_first` changed meaning, not just length.**

*Location:* `src/exomem/commands.py:1277-1279` (records) and `:1315-1317` (planning).

- **Before:** "direct human edits and work without an agent are supported product paths".
- **After:** "manual edits without Exomem are a supported path".

What is lost:
- Agent-free use of Exomem itself (CLI or TUI) is no longer covered.
- The new wording can be read as "manual edits are supported only when Exomem is not involved". That contradicts the structured-collections contract, where a direct human edit made after an agent read stays canonical.

The test comment at `tests/test_bootstrap_compact_budget.py:393-396` says the change was "shortened … losing no clause". That is not accurate. The two clauses are in different sections, so they are not duplicates.

*Reproduction:* the diff, plus a render of the payload on the PR head.

*Fix:* "direct edits and agent-free use are supported paths". This adds 8 bytes in total; maximal/claude-code then has 282 bytes of headroom, still above its 256 floor.

**2. Low — the "future observation" justification points at a section that is not always served.**

*Location:* `tests/test_bootstrap_compact_budget.py:397-402`.

The comment says `records.intent_boundary.prediction` carries the clause. On a surface without Records, that section is absent.

*Reproduction:* render with the descriptor `("bootstrap","ask_memory")`. "future observation" then appears only at `epistemic_contract.vocabulary.kinds.prediction`, and `records.available` is False.

The spec requirement is still met through `vocabulary.kinds`. However, `capture_nudge` (`commands.py:1426`) lost the qualifier the archived design used as its guard against over-applying the nudge.

*Fix:*
- Required: correct the comment to name `epistemic_contract.vocabulary.kinds.prediction`.
- Optional: restore " about a future observation" in `capture_nudge` (+27 bytes, leaving maximal/claude-code at 263 against a 256 floor).

**3. Low — the `capture_sweep_handling` justification only holds at balanced and maximal.**

*Location:* `commands.py:1681-1683` and the test comment at `:387-392`.

`engagement.contract.capture` carries the reuse bar only at balanced and maximal. At light or off, with the operator override `{"envelope":{"proactive_capture":"silent"}}`, sweep blocks are still delivered. The payload then has no reuse bar at all:

```
light: main reuse-bar-in-payload=True  PR=False
off:   main reuse-bar-in-payload=True  PR=False
```

No instruction is actually lost, because every delivered block carries `capture_sweep.RULE` (`capture_sweep.py:81`).

*Fix:* point the comment at `capture_sweep.RULE`.

**4. Nit — stale docstring and numbers.**
- `tests/test_bootstrap_compact_budget.py:491` still says the margin is asserted "only at the default level"; maximal now has its own floor.
- The commit message's 664/655 is the explicit-`balanced` figure. With no level set, it is 660/651. Both clear 512.

### Verified clean
- `test_bootstrap_activation_carrier.py` and `test_bootstrap_compact_budget.py`: 45 passed, with `-W error::UserWarning`.
- Red-first: with `commands.py` reverted, 6 failed (the default-level margin, maximal floor, compact headroom and hook-capable ceiling nodes).
- Targeted bootstrap, epistemic, capture-sweep, prominence and hosted suites: 594 passed.
- All 90 test files that mention bootstrap: 3,117 passed. The 61 failures fail identically on main (`HOSTED_RUNTIME_ID_INVALID`, environmental).
- No test, fixture or spec pins any of the removed strings. The ceiling is not raised, and `MINIMUM_SAVING_RATIO` is unchanged.
- `generate-capabilities.py --check`: current. Privacy gate: clean. Ruff F: clean.

---

## #1376 — fix(cli): refuse a working-directory .env inside a vault in every loader

**Verdict: APPROVE WITH REQUIRED CHANGES**

The shared `dotenv_guard` compares resolved paths. It recognizes both the configured vault and a structural exomem vault. Every loader that reads a `.env` from the working directory in `src/` now goes through it.

### Findings

**1. Medium (required) — `setup --remote` checks against the wrong vault and writes its secrets `.env` inside the vault it is configuring.**

*Location:* `src/exomem/remote_setup_wizard.py`.
- `:308`: the guard runs before the vault is known.
- `:344`: the vault is chosen here, from `--vault` or the prompt.
- `:541`: `env_path.write_text` writes the secrets with no second check.

*Reproduction:* call `run_remote_setup(vault=<cwd>, env_path=<cwd>/.env, yes=True, ...)` with stub doctor and loader, in a folder that is not yet a vault.

```
A wizard exit: 0 | .env written inside chosen vault: True | contains secret: True
   server-side guard on that same file: None  (event=dotenv_refused reason=inside_vault)
B wizard exit: 0 | .env written in subdir of chosen vault: True
```

Consequences:
- The GitHub client secret and signing key land inside a synced vault.
- The first run produces a configuration the service refuses to load.
- A second wizard run refuses, so behaviour flips between runs.
- The wizard's `_load_env` reads the file it just wrote, and it is exempt from the structural test.

*Smallest fix:* once `vault_path` is known and before the write, refuse when `env_path.parent.resolve()` is equal to, or under, `Path(vault_path).expanduser().resolve()`. Alternatively, give `dotenv_load_guard` an `extra_roots` parameter and call it again. Add a node with `vault=` set to the `.env` file's directory.

**2. Low (moved, not introduced) — legacy vaults with a custom KB folder name are not detected when the shell lacks `EXOMEM_KB_DIRNAME`.**

*Location:* `src/exomem/dotenv_guard.py:67-70`, via `_is_vault` / `kb_dirname()`.

*Reproduction:* a vault with only `Notes/_Schema/SKILL.md`, the working directory at its root, and no vault variables set. Result: `guard=None`, and the CLI loaded `evil://x`. With `EXOMEM_KB_DIRNAME=Notes` set, the `.env` was refused.

*Optional fix:* treat any `*/_Schema/SKILL.md` child as a vault marker.

**3. Low — doctor passes a `.env` the loader refuses.**

*Location:* `doctor.py:572-575` (`_check_repo_env`, not touched by this PR). With the working directory at a vault root that holds a `.env`: guard `None`, doctor `env.file: pass`.

*Fix:* consult `working_directory_dotenv()` and warn with the refusal remedy.

**Note (observation):** the structural test in `tests/test_remote_owner_invariants.py` exempts any function whose source text contains `working_directory_dotenv(` or `dotenv_load_guard(`, including inside a docstring. It also does not catch `parse_env(path.read_text())` readers.

### Loaders enumerated

| Loader | Status |
|---|---|
| `server_runtime.initialize_runtime` | Guarded |
| `__main__._load_cwd_dotenv` (`auth`, `doctor`) | Guarded |
| `runtime_resources.preload_local_dotenv_policy` | Guarded |
| `setup_wizard._configured_mcp_url` | Guarded |
| `remote_setup_wizard.run_remote_setup` | Guarded against the working directory only (finding 1) |
| `remote_setup_wizard._load_env` | Reloads the file the wizard wrote (finding 1) |
| `doctor._check_repo_env` | Checks existence only (finding 3) |
| `scripts/*service*.sh`, `restart.sh`, `upgrade.sh`, `install-service.*`, `set-upload-token.py` | Read `<repo>/.env` or `--env-file`, never the working directory. Out of scope. |
| `_hooks/exomem_retrieve_nudge.py` and its plugin mirror | Read the managed `service.env` only |
| `sidecar/`, `plugins/hosted`, `hosted_runtime`, `demo`, `tui/backend` | No working-directory dotenv |

### Symlink, parent and path probes

**Refused, as expected:**
- The working directory is the vault root, or a deep subdirectory of it.
- The working directory is a symlink into the vault.
- A `.env` outside the vault is a symlink into it, or a `.env` inside is a symlink out of it.
- The vault is configured through a symlinked path.
- The vault path is written with a trailing slash, as `~`, or as a relative `.`.
- The `.env` declares its own directory, or an ancestor of it, as the vault (the chicken-and-egg case).
- The vault is set only through the legacy `KB_MCP_VAULT_PATH`.
- `.env` is a directory.

**Allowed, as expected:**
- The working directory is the parent of a vault.
- A folder with only a `.obsidian` marker.
- A sibling directory sharing a name prefix (`v2` next to `v`).

### Verified clean
- The PR's 5 test files plus the scaffold and `server_runtime` suites: 175 passed.
- CLI, runtime-resource and state-root suites: 286 passed, 2 skipped.
- Red-first: with production files reverted and `dotenv_guard.py` removed, 17 failed. `test_dotenv_guard.py` fails at collection.
- No leftover references to the removed `server_runtime._working_directory_dotenv`.
- No `_hooks` file is touched. Ruff F: clean. Privacy gate: clean.

---

## #1379 — fix(fast-ack): keep owed advisories through reconcile and heal hand-deleted new pages

**Verdict: APPROVE WITH REQUIRED CHANGES**

The base is `fix/fast-ack-visibility` @ `e40971a`. No `REVIEW*.md` exists on any branch, and neither this PR nor its base has review comments. F1 and F2 are therefore taken from the PR body, which applies an independent review of `e40971a`.

- **F1:** reconcile step 3b superseded every advisory result of a batch it retired. Ready warnings then disappeared behind `superseded`.
- **F2:** a created page deleted by hand was handed on only once a newer batch recorded a tombstone. Until then, managed recall stayed `warming` across the vault until an operator ran reconcile.

### F1 and F2: fixed and pinned?

With `src/` reverted to the base, the three new and changed test modules gave 10 failed and 123 passed.

**F1 — fixed and pinned** (commit `42517d8`: reconcile now retires only receipt custody). These nodes go red without the fix:
- `test_a_finished_advisory_survives_reconcile[ready]`
- `test_a_finished_advisory_survives_reconcile[failed]`
- `test_a_pending_advisory_over_an_unchanged_page_fails_as_unavailable` (`('superseded', None) != ('failed','advisory_unavailable')`)

**F2 — fixed and pinned for the tested shape only** (commit `58d9308`). These go red without the fix:
- `test_a_new_page_deleted_by_hand_before_it_converges_heals_without_reconcile`
- `test_a_new_page_deleted_by_hand_waits_until_the_lanes_lose_it`

Finding 1 below shows a shape the fix misses.

### Findings

**1. Medium (required) — the F2 heal is lost once a batch that was proven but not published goes to `reconcile_required`.**

*Location:* `src/exomem/derived_receipts.py:1223`, `proven = str(row[1]) in {"ready","completed"} or bool(row[2])`.

"Proven" is derived from the batch's current state or from its live and retired pending rows. Take a batch proven `ready` whose pending rows are still `prepared`, for example because publication failed or the process crashed first. The first re-proof that moves it to `reconcile_required` (line 1495) erases that evidence. Recovery then keeps the batch stuck until an operator runs reconcile, even after both lanes hold the absence. That is exactly the state F2 set out to remove.

*Reproduction:*
1. `prepare_batch` with one created page, then write it.
2. `prove_committed` returns `ready` (not published).
3. `upsert_after_write` the page, then unlink it.
4. `prove_committed`, then `delete_after_remove`, then `prove_committed`, then `_drain_until_idle`.

```
o1 reconcile_required  lanes hold absence True
o2 reconcile_required  after drain ['reconcile_required']
```

Control: the same batch with the lanes emptied before the first re-proof ends `superseded`. So the outcome depends only on whether recovery or the watcher runs first.

*Smallest fix:*
- Persist the fact of proof: a `proven_at` column set in `_activate_proven_batch` and read in `_handed_on`.
- Add this sequence as a test node.

**2. Low — a brief absence while the advisory runs now supersedes its result permanently.**

*Location:* `src/exomem/deferred_write_advisory.py:373-386`. When the target is missing at observation time, the executor publishes `"absent"`, and that supersedes the result for good. On the base, the result stayed `pending` and was retried.

*Reproduction:*
1. Patch `_observe_fingerprint` so it unlinks the page on its first call.
2. Restore the page byte-identical after one `_one_pass`.
3. Drain.

Results:
- PR: `advisory ('superseded', None)`.
- Base: `advisory ('pending', None)`.

How realistic this is remains speculative: it needs an editor that saves by delete-then-rewrite, landing in that window.

*Fix:* take the absent-target branch only after the receipt proof has handed this path on (its pending row is retired or the batch is superseded). Otherwise keep retrying.

**Note (not reproduced):** in `deferred_index.py:220-225`, the `batch_seq` `ALTER` runs outside the backfill `UPDATE`'s transaction. A crash between the two would leave NULL `batch_seq` rows that can never count as coverers. Wrapping both in one explicit transaction closes it.

### Reconcile ordering and delete/recreate attacks (clean)

- **Natural stranded shape.** The first reconcile gives `{'stranded':1,'retired':1,'remaining':0}` and the advisory ends `failed/advisory_unavailable`. A second reconcile retires 0, and the state is unchanged after a drain. It is delivered exactly once.
- **Advisory for page A while reconcile retires batch B.** A's `ready` result is untouched. B's advisory, whose page moved, is `superseded`.
- **Reconcile racing four drain passes in threads.** No exceptions, and one terminal state per result. This is backed by `_settle_owed_advisory` updating only `WHERE state='pending'`, and by publication requiring a live component claim.
- **`maintain --reconcile` versus background.** Both reach the same `reconcile.reconcile` (`commands.py:3722`).
- **Deleted, then recreated with the same bytes before a drain.** `completed`, overlay `ready`.
- **Deleted, healed, then recreated with different content.** `completed`, overlay `ready`, doctor pass, no ghost or duplicate row.
- **Renamed by hand.** `completed`, advisory `superseded`.
- **Crash-cut batch whose new page was deleted while the lanes still hold it.** It stays owed, and operator reconcile retires it cleanly.

### Verified clean
- 12 fast-ack, receipt, advisory, reconcile, writer-lease, scaffold and doctor suites: 539 passed, 6 skipped.
- No `_hooks` file is touched.
- `openspec validate --all --strict`: 212 passed.
- Privacy gate: clean. Ruff F: clean.
