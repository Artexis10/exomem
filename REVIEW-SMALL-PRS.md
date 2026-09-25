# Review: four small open PRs

Independent review of #1369, #1370, #1371 and #1372 (the NFD read/edit fix,
which was open by the time the first three were done). For each PR I checked
out the head branch, read the full diff, ran its tests plus the neighbouring
suites, reverted only the production files to `origin/main` to confirm the new
tests go red, and then wrote throwaway probes for the cases the tests don't
cover. None of those probes are committed. Every reproduction below was
actually run.

| PR | Head | Verdict |
|----|------|---------|
| #1369 fix(upgrade): show uv's error tail when release staging fails | `0a309d2` | **REQUEST_CHANGES** |
| #1370 fix(hosted): clear every legacy alias of a cleared setting | `f87df9d` | **APPROVE WITH REQUIRED CHANGES** |
| #1371 fix(hooks): count an episode recorded through any door in the Stop-hook check | `474b042` | **REQUEST_CHANGES** |
| #1372 fix(read): open NFD-named pages on byte-exact filesystems | `568f715` | **REQUEST_CHANGES** |

Environment note: the `tests/test_hosted_cell.py::test_hosted_readiness_provider_uses_the_bound_control_plane_identity`
failure (`HOSTED_RUNTIME_ID_INVALID`) happens on `main` as well as on #1370. It
comes from the review container running as uid 0 and has nothing to do with
any of these PRs.

---

## #1369: uv stderr tail on staging failure

**Tests:** `tests/test_service_upgrade.py` gives 28 passed on the branch. With
`src/exomem/service_upgrade.py` reverted to `main`, the four new tests fail and
the rest pass. So the tests are red-first.

**Scope checked:** The error only reaches the operator's own stderr
(`main()` → `print(f"managed upgrade: {exc}", file=sys.stderr)`). Nothing is
written to the control socket or to durable state. That lowers the blast radius
but does not remove it, because operator stderr routinely ends up in CI logs,
cron mail and service journals. Before this PR, `main` deliberately echoed none
of this output (`test_stage_failure_does_not_echo_package_manager_output`), so
this PR is the first to let a credential through.

### F1: HIGH: the tail is clipped *before* it is scrubbed, so a clipped URL leaks its password
`src/exomem/service_upgrade.py:313-315`

The byte clip keeps the last 4096 bytes and only then runs `_URL_USERINFO_RE`,
which needs `://` in front of the userinfo. If the clip lands inside
`https://user:`, the scheme is gone, the lookbehind can't match, and the
password comes through unredacted.

Reproduction (run on the branch):
```python
url = b"https://deploy:S3cr3tTok3n9876@pypi.example.com/simple/ "
_uv_stderr_tail(url + b"y" * (4096 - len(url) + 10))
# -> 'ploy:S3cr3tTok3n9876@pypi.example.com/simple/ yyyy…'
```
The same thing happens at the line clip whenever a long line splits across the
20-line window. Any 4 KiB-clean cut can also split a token that
`scrub_text`'s entropy pass would otherwise have recognised.

**Smallest fix:** scrub the whole decoded stderr (or at least the last ~64 KiB
of it) first, then clip the scrubbed text. Also drop any leading partial line
left by the byte clip, i.e. everything up to the first `\n`, so a fragment of
a credential is never emitted.

### F2: MEDIUM: the 4 KiB bound does not hold, because scrubbing runs after the clip and expands the text
`src/exomem/service_upgrade.py:311-316`

`NOTICE` is 47 bytes, so every `u:p` it replaces makes the output longer. Measured:

| input | output bytes |
|---|---|
| `b"https://u:p@h " * 400` | **16109** |
| 30 lines of `https://a:b@h/` × 30 | **16068** |

`test_stage_failure_truncates_large_uv_stderr` only feeds in `x` bytes, which
the scrubber leaves alone, so it can't catch this.

**Smallest fix:** scrub first and clip last, which is the same reorder as F1.
Then add a test that asserts `len(tail.encode()) <= 4096` for input made of
many short credential URLs.

### F3: MEDIUM: userinfo that carries only a token is not scrubbed
`src/exomem/service_upgrade.py:35`

`_URL_USERINFO_RE` requires `user:pass@`. A token-only userinfo, which is common
for private indexes and git sources (`https://<token>@host/...`), is left alone
unless `scrub_text` happens to recognise the token's shape:
```python
_uv_stderr_tail(b"error: https://hunter2hunter2@pypi.example.com/simple/")
# -> unchanged, token included
```
**Smallest fix:** replace the entire userinfo segment whether or not it has a
colon: `re.compile(r"(?<=://)[^/\s@]+(?=@)")`. Add the token-only case as a test.

### F4: LOW: stderr is fully buffered before it is bounded
`src/exomem/service_upgrade.py:371`

`stderr=subprocess.PIPE` holds the whole stream in memory for a command that
can run for up to 900 s. uv's stderr is small in practice, so this is a
hardening note, not a blocker. **Fix (optional):** send stderr to a
`tempfile.TemporaryFile()` and read back only its last 64 KiB.

### F5: LOW: `UV_*_PASSWORD=value` style lines are not scrubbed
`UV_INDEX_PASSWORD=S3cr3tTok3n9876 failed` comes through unchanged, because
the value's entropy is too low for `scrub_text`. uv doesn't normally print
this, but a user's wrapper script can. **Fix (optional):** add a
`(?i)(password|token|secret)\s*[=:]\s*\S+` redaction next to the userinfo rule.

---

## #1370: clearing legacy aliases in hosted mode

**Tests:** on the branch, `tests/test_hosted_cell.py` gives 55 passed and 1
failure, the environment one noted above. Reverted to `main`, only the
structural test `test_every_cleared_setting_clears_its_legacy_alias` fails.
The parametrized behaviour test *cannot* fail on `main` (see F3).

**Single source of the alias table: yes.** `env_compat.promote_legacy()` is a
pure prefix swap (`KB_MCP_X` → `EXOMEM_X`) and has no rename table.
`_legacy_alias()` derives the name from `env_compat.LEGACY_PREFIX` and
`CANONICAL_PREFIX`, so the derivation is exact and can't drift. The
non-prefixed `GITHUB_CLIENT_*` entries correctly get no alias.

**Hosted startup paths.** The hosted server path
(`server_runtime.initialize_runtime` → `_initialize_hosted_runtime` →
`apply_process_environment`, and `__main__` around line 3194) returns *before*
the standalone `promote_legacy()` at `server_runtime.py:156`. In the serving
process itself, the only promotion is the one at package import, and that
runs before the clear. The re-promotion that matters happens in **child
processes**. They inherit `os.environ`, and their own `import exomem` runs
`promote_legacy()` again. That is the mechanism this PR closes for the list of
cleared settings. The same mechanism still reopens names that the gate helpers
clear, which is F1.

### F1: MEDIUM: canonical names that the gate helpers pop are re-armed in child processes by their legacy spelling
`src/exomem/hosted_runtime.py:528-537` (`_apply_truthy_gate`) and `:508-527` (`_apply_disable_gate`), with the helpers at `:1454-1465`

`_apply_truthy_gate(target, False, "EXOMEM_DIARIZE")` pops `EXOMEM_DIARIZE`
every time. `EXOMEM_VISION_CAPTION` is popped whenever vision isn't granted.
Neither legacy spelling is cleared.

Reproduction (run on the branch, with no feature grants):
```
parent after apply:       EXOMEM_DIARIZE=None  EXOMEM_VISION_CAPTION=None
child after import exomem: EXOMEM_DIARIZE='1'  EXOMEM_VISION_CAPTION='1'
```
The input was `KB_MCP_DIARIZE=1` and `KB_MCP_VISION_CAPTION=1` in the
inherited environment. A worker subprocess therefore runs with diarization
and captioning turned on, which the hosted grant model explicitly denies.
`_apply_disable_gate` has the reverse, fail-safe version of the problem: a
granted feature whose `EXOMEM_DISABLE_*` is popped gets turned back off by a
stray `KB_MCP_DISABLE_*`. That is annoying but not a security problem.

This is the same class of bug the PR title promises to fix ("every legacy
alias of a cleared setting"), so it belongs in this PR.

**Smallest fix:** have both gate helpers also
`target.pop(env_compat.LEGACY_PREFIX + variable[len(env_compat.CANONICAL_PREFIX):], None)`.
Better still, have `apply_process_environment` pop the legacy alias of
*every* `EXOMEM_*` name it touches, including the names it sets, using
`_legacy_alias`. Add one test that applies the environment to a dict holding
`KB_MCP_DIARIZE=1`, then runs `promote_legacy`-equivalent logic, and asserts
that `EXOMEM_DIARIZE` stays absent.

### F2: LOW: the names hosted mode *sets* are safe only because they are set
`EXOMEM_VAULT_PATH`, `EXOMEM_STATE_ROOT` and similar are written, so promotion
never overrides them. That's correct today, but it depends on ordering, and
the PR's comment doesn't say so. The broader fix in F1 (pop the legacy alias of
every name touched) makes it structural.

### F3: LOW (test quality): the parametrized test is generated from the implementation
`tests/test_hosted_cell.py:463-488`

`test_hosted_mode_clears_every_legacy_alias` parametrizes over
`name in hosted_runtime._HOSTED_CLEARED_ENV if name.startswith("KB_MCP_")`. If
the derivation dropped an alias, that alias would simply stop being a test
case. On `main` it collapses to one case that passes. Likewise,
`test_every_cleared_setting_clears_its_legacy_alias` repeats the production
formula. **Smallest fix:** parametrize over the canonical settings instead
(`_HOSTED_CLEARED_SETTINGS`, or better a literal list). Put the *legacy*
spelling into the env, apply, run `env_compat.promote_legacy()` against a
patched `os.environ`, and assert the canonical name is absent. That is the
real behaviour, and it fails on `main` for 15 of the 16 names.

---

## #1371: Stop-hook check for episodes recorded through any door

**Tests:** 176 passed on the branch (`test_capture_nudge_episode.py`,
`test_install_parity.py`, `test_install_hook.py`). The mirrors are
**byte-identical** (`cmp` of `src/exomem/_hooks/exomem_capture_nudge.py` against
`plugins/claude-code/hooks/exomem_capture_nudge.py`). Reverted to `main`, the
REST-suppression test fails for the right reason. The four fallback tests and
`test_a_transcript_record_still_counts_without_consulting_the_door` also
fail, but only with `AttributeError: … has no attribute 'urllib'`, i.e.
because of the monkeypatch target, not because of behaviour. They are
regression guards, not red-first tests, which is fine as long as they are
described that way.

**Timeout bound: holds.** The socket timeout is `urlopen(timeout=2.0)` and
the wall-clock bound is `_bounded(…, 2.0)` on a daemon thread, so a hanging
door costs at most about 2 s, and only on a Stop where the ask was about to
fire. The only unbounded step is `_resolve_rest_key()` reading `service.env`
(line 566) outside the thread. If `EXOMEM_SERVICE_ENV` points at a FIFO, the
Stop hangs. That's an edge case (LOW, optional: move the key resolution inside
`_call`).

**Failure fallback: holds.** Any exception, a non-200 response, a malformed
body, `success != true` (which covers `EPISODE_NOT_FOUND`) or a timeout
returns `None`, and `None` leaves today's cadence unchanged. An unresolved key
makes no call at all.

**What inspect asks for and leaks: nothing beyond the count leaves the hook.**
The request body is exactly `{"action":"inspect","episode":<key>}`. The
response carries `revisions[{revision, recovery}]`, `latest_source_ref` (a
vault path) and `coverage_current`. The hook keeps only `len(revisions)`,
persists only that integer, and prints nothing from the response. The bearer
key is sent only to `EXOMEM_HOST`. A key lifted from `service.env` is limited
to loopback, while a key exported in the environment follows
`EXOMEM_HOST`, matching the retrieve hook. Two LOW notes: the default opener
follows redirects and re-sends `Authorization` to the redirect target, so a
loopback listener could bounce a file-sourced key off-host (the retrieve hook
has the same behaviour); and a remote `EXOMEM_HOST` receives the bearer over
plain `http`. Neither is new with this PR.

### F1: HIGH (behaviour regression): every record the hook sees in the transcript makes the next due ask get swallowed
`src/exomem/_hooks/exomem_capture_nudge.py:672-692` (the sync is missing at 672, and the comparison at 687 is wrong as a result)

When `_successful_episode_record` resets `substantive_since_record`,
`last_seen_revisions` is left stale. The next time the ask comes due, the door
reports that same record as a revision (`1 > 0`), so the ask is suppressed and
the counter resets again. After every record made through the hook's own
door, the user therefore waits **2K** substantive turns instead of K. On
`balanced` that is 12 instead of 6. The unit tests miss it because the
transcript-record test forbids the door call, and the REST test starts with
no prior record.

Reproduction (run on both branches):
```python
# door reports count["n"] revisions
_stop(transcript with successful mcp__exomem__episode_memory record)
count["n"] = 1                        # that record is now in the ledger
results = _stops(..., K)              # K substantive Stops
assert _is_episode_ask(results[-1])
# main: passes.   PR: FAILS (the capture reminder fires instead of the ask)
```
**Smallest fix:** when a transcript record is seen, mark the baseline as
unknown so the next door read re-bases instead of suppressing. For example,
set `state["last_seen_revisions"] = -1` in the reset branch. At the door
check, if `last_seen_revisions < 0`, store `revisions` and **don't** suppress.
Only a count above a known baseline should suppress. Add the reproduction
above as a test.

### F2: LOW: "any door" only covers doors in the same audience
`episode_memory.inspect` is per audience: a key held only by another audience
returns `EPISODE_NOT_FOUND`. A recap recorded under a different principal from
the REST key's is invisible to this check. That fails safe (the ask just
fires), but the module docstring and PR title overclaim. **Fix:** say "any door
acting as the same audience as the REST key" in the docstring.

### F3: LOW: a baseline that only goes up hides new revisions after a reset
`last_seen_revisions = max(old, new)`. If the ledger ever reports fewer
revisions (for example after restoring the vault), later revisions from other
doors stay invisible until they pass the old high-water mark. **Fix
(optional):** store `revisions` rather than `max(…)`. With F1's fix,
suppression already requires a strictly higher count than the last value read.

---

## #1372: NFD read/edit on byte-exact filesystems

**Tests:** 519 passed across `test_nfd_page_access.py`,
`test_get_edit_roundtrip.py`, `test_replace.py`, `test_governance_egress.py`,
`test_graph_class_c_evidence.py` and `test_reserved_admin_paths.py`.
Reverted to `main`, 8 of the 9 new tests fail. The withheld-equals-absent test
passes on `main`, as it should for a guard. CI was still running when I
checked.

I ran seven adversarial probes against the branch, and all seven fail:

### F1: HIGH: an AMBIGUOUS_PATH collision is an existence oracle for withheld pages
`src/exomem/get_page.py:235-242`

The new `AMBIGUOUS_PATH` branch raises straight away. It doesn't go through
`unreadable_or_absent()`, the path that maps a withheld page to `NOT_FOUND`.
A caller denied by governance can therefore tell "this withheld page exists in
two spellings" apart from "no such page":
```
withheld (NFC + NFD collision, external audience denied Notes/**):
    'AMBIGUOUS_PATH: … matches more than one on-disk spelling; refusing to guess which'
absent:
    'NOT_FOUND: file does not exist: …/café-nfd-probe.md'
```
That breaks the withheld-equals-absent invariant the PR claims to keep, and
the PR's own test covers only the single-file case.

**Smallest fix:** in the `ReservedPathLeafError` handler, send
`AMBIGUOUS_PATH` through `raise unreadable_or_absent(vault_root, (resolution.relative, resolution.resolved_relative), missing_path, reason)`
(it returns a `GetError`) the same way the `UNREADABLE` family is handled. It
currently returns `UNREADABLE` for a released path, so either extend it to
carry the code through or check the withheld case first, so a withheld path gives
`NOT_FOUND` and only a released path gets `AMBIGUOUS_PATH`. Apply the same
rule to the edit, replace and move doors (F3). Add the collision-plus-deny
case to `test_withheld_nfd_page_reads_as_missing_to_restricted_caller`.

### F2: HIGH: edit and replace rename the file on disk before validation or authorization, including under `validate_only=True`
`src/exomem/edit.py:441-484` and `src/exomem/replace.py:328-371`

`_resolve` and `_resolve_kb_path` call `move_generic_path(physical_rel → canonical_rel)`
as a *side effect of resolving the path*. That happens before the semantic
contract, before any governance check and before the dry-run branch.
Reproductions (run on the branch):
- `edit(..., validate_only=True)` on an NFD page leaves the NFD file **renamed**.
- `edit(..., new_body="")`, refused with `SEMANTIC_CONTRACT_BLOCKED`, leaves the
  file **renamed**.
- Under `request_scope(external)` with a deny rule on `Notes/**`, a call to
  `edit.edit()` on the withheld NFD page raises `SEMANTIC_CONTRACT_BLOCKED`
  **and renames the page**. I did not trace whether the MCP/REST write door
  stops a non-owner earlier, so whether a restricted caller can reach this in
  practice is still open. The mutation-before-refusal is confirmed either way.

The rename also writes no activity-log entry and no receipt, and it happens
outside the edit's own commit. **Smallest fix:** in `_resolve` and
`_resolve_kb_path`, only *resolve* (return `physical_rel`, and open it with
`physical=True` for the read and hash). Do the canonicalizing rename inside
the commit, after validation and authorization, and never when
`validate_only`. If that's too big for this PR, refuse
(`NON_CANONICAL_NAME` or similar) on the write doors and ship read-only NFD
support now.

### F3: MEDIUM: write doors don't refuse collisions when the NFKC spelling exists
`src/exomem/edit.py:441`, `src/exomem/replace.py:328`, `src/exomem/move_file.py:224`

The physical-spelling lookup runs only when the NFKC name is *missing*
(`if not candidate.exists()`, or `if e.code != "NOT_FOUND"` in move). With both
`café-nfd-probe.md` (NFC) and its NFD twin on disk:
- `op_get` refuses with `AMBIGUOUS_PATH` (correct)
- `edit` **silently edits the NFC file** (probe: "DID NOT RAISE")
- `move_file` **silently moves the NFC file** (probe: "DID NOT RAISE")

That contradicts the PR body ("refusing outright … even when one of them is
the NFKC-exact name"), and read and write now disagree about the same path.
**Smallest fix:** call `resolve_physical_relative` unconditionally in all
three doors, as `get_page` does, and map `AMBIGUOUS_PATH` as described in F1.

### F4: LOW (scope): NFD *directory* names are still unreachable
`src/exomem/reserved_paths.py:1050` (`filesystem.parent(parent_path)` with the NFKC parent)

`Knowledge Base/Notes/<NFD "Café">/plain.md` still gives `NOT_FOUND` from
`op_get`. macOS-origin folder names are as likely to be NFD as file names are.
This is an out-of-scope follow-up, not a blocker. At minimum, the PR body
should say that only leaf names are handled.

### Note: egress ordering
The release decision is still made on the canonical path string, and the
swap-check in `annotate_page` still compares against the immutable `raw`, so
the physical lookup doesn't change *what* gets decided. The PR body's claim
that the decision is made "before any bytes are read" doesn't match
`get_page`, though. Apart from `access.refuse_if_excluded`, `prepare_page_read`
reads the bytes first, and `annotate_page` decides afterwards. That ordering
existed before this PR, and it's safe because the decision is keyed on the
path and the snapshot is immutable. The new code adds one directory listing
before the decision, and that listing leaks only through the
`AMBIGUOUS_PATH` error covered by F1. Please correct the wording in the PR
body.

---

## Summary of required changes

- **#1369**: scrub before clipping and drop the leading partial line (F1).
  After that the bound holds (F2). Redact token-only userinfo (F3).
- **#1370**: clear the legacy spelling of the names the gate helpers pop, at
  least `EXOMEM_DIARIZE` and `EXOMEM_VISION_CAPTION` (F1). Make the behaviour
  test independent of the derivation it is checking (F3).
- **#1371**: re-base `last_seen_revisions` after a record the hook sees in the
  transcript, so its own records don't swallow the next ask (F1).
- **#1372**: send `AMBIGUOUS_PATH` through the withheld-equals-absent mapping
  (F1). Stop renaming during resolution (F2). Refuse collisions on every door,
  not just on read (F3).
