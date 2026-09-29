# Security review: attachment custody (PR #1436, `5dc83545..827d631`)

**Verdict: REQUEST_CHANGES.** There is one Medium finding, a transcription recorded against the wrong original. The handle, ingress and storage design holds up.

## Tests and CI

- `tests/test_attachment_custody.py`, `test_client_artifacts.py`, `test_upload_endpoint.py` and `test_local_ingress_{cli,e2e,listener,worker}.py`, run with `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d)` and the pinned uv 0.11.28: **241 passed**, 0 failed.
- PR CI on `827d631`: **required CI gate: success.** Core shards 1–12, harness, lint, OpenSpec, capabilities, build and E2E all passed. The conditional jobs were skipped. The PR is still a **draft**.

## Findings

### M1. A transcription can be recorded on a different original's page

`src/exomem/client_artifacts.py:1665` (`_transcriptions_by_file`) and `:1534`.

Transcriptions are keyed by the `file_id` the caller supplies. `preserve_artifacts` never checks that each `file_id` in `files` is unique, and a held handle's `file_id` is only a label: the caller can rewrite it, because the secret alone decides redemption. If two files share a `file_id`, one transcription is written to **both** pages. Each page gets `extracted_by: client-transcription` and a `transcription:` record carrying *that page's own* SHA-256. The page then states that text derived from original A was derived from B's exact bytes.

**Reproduction** (probe, local session `s1`):
1. Hold PNG A as `card.png` and PNG B as `other.png`.
2. Set `hb["file_id"] = ha["file_id"]`.
3. Call `preserve_artifacts(files=[ha, hb], transcriptions=[{file_id: ha.file_id, text: "TEXT-OF-A"}])`.

Result: both rows are `stored` with `transcription.state == "recorded"`. `other.png.md` contains `TEXT-OF-A`, and its `transcription.sha256` is B's hash. Remote (ChatGPT) handles behave the same way.

**Minimal fix:** reject the call before staging when `transcriptions` is non-empty and `files` repeats a `file_id`. Use `_refuse_transcriptions("file_id values must be unique when transcriptions are supplied")`. Add a red-first test that passes two handles with the same `file_id`.

### L1. A preserve failure after the claim spends the hold

`src/exomem/held_uploads.py:242`, then `client_artifacts.py:1615`.

A redemption *refusal* (wrong lane, byte budget) leaves the hold in place. But once `os.rename` claims the bytes, any later per-file failure deletes the claimed file in the `finally`. That includes a filename collision, which a probe hit. The client must attach again. This affects availability only.

**Fix:** document "spent once claimed" in the tool description. Alternatively, rename the claimed file back when the per-file outcome is `failed` before any write.

### L2. Hold storage has no quota, and expired holds are swept lazily

`held_uploads.py:106` and `:146`.

`_sweep` runs only inside `hold()`. Expired holds stay on disk until the next hold. Nothing limits how many holds one session may keep: each may be up to `upload_max_bytes` and lives for an hour. The caller is an authenticated same-machine session, so the disk-exhaustion risk is self-inflicted.

**Fix:** also sweep in `redeem()`, and cap total held bytes or hold count per binding.

### I1. An existing hold directory's mode is not checked

`held_uploads.py:86`. `mkdir(mode=0o700, exist_ok=True)` does not re-check a directory that already exists. The parent is 0700 and every file inside is 0600, so this is informational.

### I2. No test redeems through MCP transport

The end-to-end test redeems over REST `/api/preserve_artifacts`. Redeeming over MCP depends on the grant ContextVar reaching the tool task. That holds under `stateless_http=True`. One MCP-path test would pin it down.

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
4. **Transcription only with a stored original:** a failed or `already_stored` original records `not_recorded` and leaves the page unchanged, which is tested. Unknown `file_id`s are refused before staging. An adoption rejects transcriptions.
   **Labelling:** a transcription is labelled `extracted_by: client-transcription`, never `upload` or an engine name. The one gap in attribution is M1.
5. **No local paths in tool schemas:** tested by `test_no_tool_schema_takes_a_local_path_for_a_file`. `exomem attach` reads the file on the client side and sends bytes only.
6. **`.md` upload returning 500 `GraphEpochIncoherent`:** **fixed** in `919497f`. A staged UTF-8 stream is admitted using its SHA-256, and red-first tests cover both the public and held paths. The PR body records the remaining gap: a `.md` that is not valid UTF-8 still returns 500 and writes nothing.

Note: rename or squash the `fa5311e wip:` commit before merge. Task 6.1 is open, so the change is correctly unarchived.

## Recheck at 7b689dec (e58c8ae5 + 7b689dec)

**Verdict: APPROVE for the code, with one merge condition.** M1, L1, L2, I1
and I2 are fixed. The `fa5311e wip:` commit is still on the branch, and
renaming it needs a history rewrite, which this lane does not do. Merge by
squash, or have the owner rewrite it first. The new concerns are Low or
informational.

### Findings

- **M1: FIXED.** `client_artifacts.py:1673-1681` refuses the call before
  staging when transcriptions are supplied and `files` repeats a `file_id`
  (labels are stripped first). Transcriptions are now keyed by position,
  not by label (`:1682-1703`, used at `:1556` and `:1562`).
  - Red-first: the test (`tests/test_attachment_custody.py:483`) is in the
    **same commit** as the fix. Against e58c8ae5^'s source it fails with
    `DID NOT RAISE OpError`.
  - Probes:
    - A duplicate label with one transcription, the second label padded
      with whitespace: refused, `INVALID_PRESERVE`.
    - Duplicates with no transcriptions, or with `transcriptions=[]`: both
      `stored`.
    - A transcription naming no file: refused, and the hold is not
      consumed.
- **L1: FIXED.** On failure, `_release_staged` (`client_artifacts.py:383-403`)
  and `held_uploads.restore` (`held_uploads.py:331-353`) put the claimed bytes
  back as the same hold. The budget path at `client_artifacts.py:367-372` does
  the same. Both lanes are tested (`test_attachment_custody.py:511`, `:534`),
  and the rule is documented at `commands.py:8060`.
  - Probe: after a filename collision (`ARTIFACT_EXISTS`), the handle
    redeemed under a new name as `stored`. A third use was refused with
    `HELD_UPLOAD_UNAVAILABLE`.
  - `already_stored` spends the hold, as documented.
- **L2: FIXED.** `redeem()` sweeps (`held_uploads.py:275`). Each binding is
  capped at 16 holds or 256 MiB (`:48-50`, `:204-206`). The byte cap is
  checked per chunk before the write (`:219-220`), so a breach leaves no
  `.part` file (tested at `:604`).
- **I1: FIXED.** `held_uploads.py:99-106` uses `lstat`, requires a directory,
  refuses one owned by another user and resets the mode to 0700. Tested at
  `:621`.
- **I2: FIXED.** `test_a_hold_is_redeemed_over_the_mcp_transport`
  (`test_attachment_custody.py:692`) redeems through `tools/call
  preserve_artifacts` on `/mcp`. It is coverage only, so it also passes on the
  parent.
- **wip commit: NOT FIXED.** `fa5311ee wip: checkpoint in-progress correction
  round` is still in `5dc83545..7b689dec`.

### Tests

- Test files: the seven original files plus `test_attachment_source_ingestion`,
  `test_mcp_schema_fidelity`, `test_tool_surface_contract`,
  `test_tool_surface_fingerprint`, `test_hosted_agent_surface` and
  `test_connector_guardrails`.
  - **366 passed, 0 failed** (pinned uv 0.11.28, temporary `XDG_STATE_HOME`).
  - The original seven files now collect 249 tests, up from 241.
- Of the new tests, 7 fail against e58c8ae5^'s source: M1, the two restores,
  the redeem sweep, the count cap, the byte cap and the directory mode.

### Gates and derived artifacts

| gate | result |
|---|---|
| `generate-capabilities.py --check` | current |
| `hosted-plugin.py check` | pass |
| `hosted-plugin.py check --candidate hosted-alpha-agent-v5 --platform all` | pass: the committed v5 locks match regeneration |
| `ruff check --select F src tests` | clean |
| `validate-public-artifacts.py --repository` | clean |

The schema baseline (`tests/fixtures/mcp_tool_schemas.json`),
`tool_surface_contract.json` and the pending digest agree with the fidelity,
contract and fingerprint tests. I did not run a separate regenerate-and-diff of
the baseline.

### CI

36 check runs on `7b689dec`: 27 succeeded, 9 were skipped (conditional) and
none failed. The required CI gate passed (run 36495224312). So did core shards
1-12, harness shards 1-4, lint, capabilities, OpenSpec, build, E2E, onboarding,
TUI, Windows NTFS and the Conventional Commit title.

### NEW CONCERNs

1. **Low: the quota check can be raced.** `held_uploads.py:204` reads usage
   once, before streaming, without a lock. With the count cap at 2, eight
   concurrent `hold()` calls from one session left four holds; the exact count
   depends on timing. The byte cap at `:219` has the same snapshot race. Each
   hold is still bounded by `upload_max_bytes`, and the caller is the
   authenticated local session.
2. **Low: `restore` skips the quota.** `held_uploads.py:331-353` restores
   without checking the cap, and `_usage` (`:144`) does not count a hold that
   has been claimed and is in flight. Probe: cap 1, claim, hold another file,
   fail the first; two live holds remain.
3. **Info: the quota refusal returns 400.** `server_transfer.py:332-335` maps
   `HELD_UPLOAD_QUOTA` to 400. A 429 or 507 would tell the client to retry
   later.
