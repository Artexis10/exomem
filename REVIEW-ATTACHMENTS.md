# Security review: attachment custody (PR #1436, `5dc83545..827d631`)

**Verdict: REQUEST_CHANGES.** There is one Medium finding, a transcription recorded against the wrong original. The handle, ingress and storage design holds up.

Scope: OpenSpec `preserve-attachment-originals`, capability `client-artifact-preservation`. The head is `827d631`. The review reads the code and adds throwaway probes. Nothing was committed to the reviewed branch.

## Tests and CI

- `tests/test_attachment_custody.py`, `test_client_artifacts.py`, `test_upload_endpoint.py` and `test_local_ingress_{cli,e2e,listener,worker}.py`, run with `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d)` and the pinned uv 0.11.28: **241 passed**, 0 failed.
- PR CI on `827d631`: **required CI gate: success.** Core shards 1–12, harness 4/4, lint + types, OpenSpec validation, capabilities doc, package build, product E2E, onboarding, TUI and the Conventional Commit title all passed. The conditional jobs were skipped. The PR is still a **draft**.

## Findings

### M1. A transcription can be recorded on a different original's page

`src/exomem/client_artifacts.py:1665` (`_transcriptions_by_file`) and `:1534`.

Transcriptions are keyed by the `file_id` the caller supplies. `preserve_artifacts` never checks that each `file_id` in `files` is unique, and a held handle's `file_id` is only a label: the caller can rewrite it, because the secret alone decides redemption. If two files share a `file_id`, one transcription is written to **both** pages. Each page gets `extracted_by: client-transcription` and a `transcription:` record carrying *that page's own* SHA-256. The page then states that text derived from original A was derived from B's exact bytes. This breaks the requirement that a transcription is bound to the original it came from.

**Reproduction** (probe, local session `s1`):
1. Hold PNG A as `card.png` and PNG B as `other.png`.
2. Set `hb["file_id"] = ha["file_id"]`.
3. Call `preserve_artifacts(files=[ha, hb], transcriptions=[{file_id: ha.file_id, text: "TEXT-OF-A"}])`.

Result: both rows are `stored` with `transcription.state == "recorded"`. `other.png.md` contains `TEXT-OF-A`, and its `transcription.sha256` is B's hash. Remote (ChatGPT) handles behave the same way, since `file_id` comes from the client there too.

**Minimal fix:** reject the call before staging when `transcriptions` is non-empty and `files` repeats a `file_id`. Use `_refuse_transcriptions("file_id values must be unique when transcriptions are supplied")`. A stricter alternative is to refuse duplicate `file_id`s in every batch. Add a red-first test that passes two handles with the same `file_id`.

### L1. A preserve failure after the claim spends the hold

`src/exomem/held_uploads.py:242`, then `client_artifacts.py:1615`.

A redemption *refusal* (wrong lane, byte budget) leaves the hold in place. But once `os.rename` claims the bytes, any later per-file failure deletes the claimed file in the `finally`. That includes a filename collision with different bytes, which the first probe run hit. The client then has to run `exomem attach` again. The effect is availability only, not a security problem.

**Fix:** document "spent once claimed" in the tool description. Alternatively, rename the claimed file back when the per-file outcome is `failed` before any write.

### L2. Hold storage has no quota, and expired holds are swept lazily

`held_uploads.py:106` and `:146`.

`_sweep` runs only inside `hold()`. Expired holds stay on disk until the next hold. Nothing limits how many holds one session may keep: each may be up to `upload_max_bytes` and lives for an hour. Starlette also spools the multipart file to the temp directory before the hold stream is checked. This is pre-existing behaviour of `/upload`. The caller is an authenticated same-machine session, so the disk-exhaustion risk is self-inflicted.

**Fix:** also sweep in `redeem()`, and cap total held bytes or hold count per binding.

### I1. An existing hold directory's mode is not checked

`held_uploads.py:86`. `mkdir(mode=0o700, exist_ok=True)` does not re-check a directory that already exists. The parent state directory is created 0700, and every file inside comes from `mkstemp` (0600), so this is informational. `os.chmod(store, 0o700)` would make it explicit.

### I2. No test redeems through MCP transport

The end-to-end test redeems over REST `/api/preserve_artifacts`. Redeeming over MCP depends on the grant ContextVar reaching the tool task. That holds under `stateless_http=True`, because anyio copies the request context into the per-request server task. A single MCP-path test would pin this down.

## Hypotheses cleared

1. **Handle entropy:** the secret is `token_urlsafe(32)` (256 bits), checked by a strict 43-character regex. Only `sha256(secret)` is stored on disk. The echoed `file_id` is 16 hex characters of that hash and cannot redeem.
   **Single use:** the atomic `os.rename` decides the winner. A probe with 8 concurrent threads saw exactly one success and seven `HELD_UPLOAD_UNAVAILABLE`.
   **Binding:** the hold is bound to `mcp-local:<session_id>` and compared with `hmac.compare_digest`. Another local session, the public or tunnel path and a hosted cell all have no grant or a different one, and all are refused. The e2e test covers the foreign-session and public cases.
   **TTL:** checked at redemption.
   **Uniform refusal:** unknown, malformed, foreign, expired and spent handles all get one code and one reason. `HELD_UPLOAD_LANE` and `HELD_UPLOAD_CHANGED` are only reachable by the owning session.
2. **Reaching `hold=1` without local ingress:** a grant exists only after `LocalIngressMiddleware` verifies the stamp, the per-process HMAC proof and a local-issuer bearer. The supervisor strips inbound `x-exomem-internal-*` headers on both listeners. A stamp without the proof gets 403. The REST key and upload token are refused on stamped requests, and on public requests `hold()` returns 400 `HELD_UPLOAD_LOCAL_ONLY`, which is tested.
3. **Path traversal:** on-disk names are a hex hash plus `mkstemp`, never input. The filename metadata is reduced to its basename, and `_sanitize_filename` still guards the vault path.
   **Symlinks:** `mkstemp` uses `O_EXCL` inside a 0700 tree.
   **Size limit:** the stream is cut at `max_bytes`, and the budget is admitted before the claim.
   **Crash leftovers:** stray `.part` and `.claimed-*` files are swept after 2×TTL.
4. **Transcription only with a stored original:** a failed or `already_stored` original records `not_recorded` and leaves the page unchanged, which is tested. Unknown `file_id`s are refused before staging. An adoption rejects transcriptions.
   **Labelling:** a transcription is labelled `extracted_by: client-transcription`, never `upload` or an engine name. Backfill treats it as done, and media reprocessing touches only `pending` pages. The one gap in attribution is M1.
5. **No local paths in tool schemas:** tested by `test_no_tool_schema_takes_a_local_path_for_a_file`. `exomem attach` reads the file on the client side and sends bytes only.
6. **`.md` upload returning 500 `GraphEpochIncoherent`:** **fixed** in `919497f`. A staged UTF-8 stream is admitted using its SHA-256, and red-first tests cover both the public and held paths. The PR body records the remaining gap: a `.md` that is not valid UTF-8 still returns 500 and writes nothing.

Note: the commit `fa5311e wip: checkpoint…` only ticks OpenSpec tasks. It should be squashed or reworded before merge. Task 6.1 (live acceptance) is still open, so leaving the change unarchived is correct.
