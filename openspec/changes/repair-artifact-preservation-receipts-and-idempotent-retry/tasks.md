## 1. Terminal persistence precedes derived acknowledgement (writer lease)

- [ ] 1.1 Red first: fault-injection test in `tests/test_writer_lease*.py` / `tests/test_mutation_terminal*.py` where the derived acknowledgement raises after a canonical commit; assert today's `MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN`, then the target: success terminal, `derived_sync="pending"`, warning naming the component class, and a same-identity retry replaying the persisted terminal without re-executing the leaf.
- [ ] 1.2 Reorder `writer_lease.py` so `_persist_completed_from_canonical` runs before the derived acknowledgement; project acknowledgement failure or budget exhaustion into the persisted terminal (`derived_sync`, warnings) and update the persisted record when acknowledgement later succeeds within the call.
- [ ] 1.3 Keep committed-uncertain for terminal-persistence failure: red-first test that injects a storage error in `_persist_completed_from_canonical` and asserts the unchanged committed-uncertain error and the canonical-resume retry.
- [ ] 1.4 Bound the acknowledgement step by the remaining request budget minus a fixed delivery reserve (constant, one module, PROVISIONAL), so a slow index or graph fan-out cannot consume the whole 60 s origin budget after the terminal is safe.

## 2. Per-file terminal states and content-hash idempotency

- [ ] 2.1 Red first: `preserve_artifacts` batch returns `request_id`, `receipt_id`, per-file `state` (`stored` / `already_stored` / `failed`) with path, ref, hash, hash_algorithm, size, media_id, content_type, warnings; summary counts all three; `outcome` mirrors `state` for one release (`already_stored` → `outcome="stored"` with `duplicate_of`).
- [ ] 2.2 Red first: duplicate bytes under a new identity yield `already_stored` naming the existing path and ref; no new file or sidecar; audit chain unchanged; identical bytes under a different category are stored (two facts).
- [ ] 2.3 Implement the destination-scoped sha256 lookup over sidecars in `client_artifacts.py` after staging and before commit; apply the same states to `preserve_evidence`.
- [ ] 2.4 Mixed batch: middle file fails, first and third store; per-file states and summary are exact; existing scenario tests stay green.

## 3. Destination segments are validated, never normalised

- [ ] 3.1 Red first: `scope="food/caviarhouse-group-order"` refuses with `INVALID_PRESERVE`, `details.field="scope"`, reason naming `/`, accepted form in the message; zero handles fetched (assert the fetcher is never called) and no Evidence directory created. Same for a category with `:` or a control character, and for leading/trailing whitespace.
- [ ] 3.2 Replace sanitisation of `scope`/`category` in `_validate_destination` and the destination join with strict single-segment validation in `client_artifacts.py` and the `preserve.py` entry used by `preserve_evidence`; keep `_sanitize_segment` for filenames.
- [ ] 3.3 Valid segments are used byte-for-byte; existing preservation tests stay green.

## 4. Hosted error shape and ledger targets

- [ ] 4.1 Red first: `_hosted_mutation_error_details` returns `status="uncertain"`, `committed=None` for `MUTATION_OUTCOME_UNKNOWN`; add a test that enumerates the terminal's status-bearing codes and asserts each has a shape entry.
- [ ] 4.2 Add the shape entry in `server_hosted.py`.
- [ ] 4.3 Red first: the ledger row for a mixed `preserve_artifacts` batch carries the committed vault-relative paths in `target_paths` and no URL, handle id or content; extend the "no note content reaches the ledger" CI test with a file handle fixture.
- [ ] 4.4 Hand outcome paths to the ledger from the `preserve_artifacts`, `preserve_evidence` and `capture_source` command wrappers.

## 5. Contract text and derived artifacts

- [ ] 5.1 `src/exomem/_scaffold/_Schema/references/mutation-results.md` (and the plugin copy): the three per-file states, the same-identity retry rule for a lost acknowledgement, and that a duplicate under a new identity is reported rather than stored twice; one paragraph, no nudging.
- [ ] 5.2 Regenerate in order and commit together: `exomem package-skills --plugin-root plugins/claude-code`; `scripts/hosted-plugin.py render`; `scripts/hosted-plugin.py render --candidate hosted-alpha-agent-v5 --platform all --openai-app-id <id>` and `check`; `scripts/generate-capabilities.py` then `--check`; tool-surface contract sha and `tests/fixtures/mcp_tool_schemas.json`; `deploy/chatgpt/personal-plugin-contract.json` pending digest.

## 6. Verification

- [ ] 6.1 Scoped gates during iteration: `tests/test_writer_lease*.py tests/test_mutation_terminal*.py tests/test_client_artifacts*.py tests/test_preserve*.py tests/test_server_hosted*.py tests/test_call_ledger*.py tests/test_tool_surface_fingerprint.py tests/test_mcp_schema_fidelity.py tests/test_plugin_sync.py tests/test_personal_baseline_contract.py tests/test_hosted_v5_candidate.py`, each run with `XDG_STATE_HOME` pointed at a scratch directory.
- [ ] 6.2 Completion boundary: full suite in the CI shard layout (12 `EXOMEM_TEST_TIER=core` + 4 `harness` pytest-split shards), failure-name diff against the branch base, `uvx ruff check . --select F`, CI full-ruff file list, `uvx mypy` CI file list, `openspec validate --all --strict` with the CI pin (`infra/tool-versions.env`), `scripts/check_openspec_archive_discipline.py`, `scripts/validate-public-artifacts.py --repository`, `scripts/generate-capabilities.py --check`; record counts here.
- [ ] 6.3 (optional, post-deploy, operational) Live vault: move the two `Evidence/foodcaviarhouse-group-order/...` files under `Evidence/food/caviarhouse-group-order/` through governed tooling and confirm the ledger records the repair; recorded for closure evidence only.
