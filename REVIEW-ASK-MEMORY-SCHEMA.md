# Review: fix/ask-memory-output-schema (PR #1448) at 78eae3bf

**Verdict: APPROVE.** The fix is correct and scoped, and the frozen pin holds. The findings are non-blocking.

## Verification

- Scoped pytest (pin, conformance, schema fidelity, tool surface, immutability, hosted rendering/v5/command-binding/legacy pin/gateway): **233 passed, 30 skipped, 0 failed**.
- `hosted-plugin.py check`: current. `generate-capabilities.py --check`: current. The privacy gate is clean. `ruff --select F` is clean.
- **End to end through the real client validator.** A throwaway probe ran `fastmcp.Client` → `mcp.ClientSession.validate_tool_result` against the built server, covering hits, empty/degraded, deep pack, explain, include_timings, a `RetrievalIndexWarming` injected into `op_find`, and a structured `OpError` governance refusal. All validate. An invalid `continuation` raises a `ToolError` (`isError=true`). The client skips validation for error results (`session.py:1101`), so that path can't trip the bug.
- **Red on main confirmed.** The warming envelope fails validation against main's v5 `ask_memory` schema.
- **Frozen candidates.** `git diff origin/main` shows no change under `generated/candidates/hosted-alpha-agent-v{2,3,4}` or the v1 root. The pin bytes equal main's v4 `ask_memory` `outputSchema`. All four pin files hash to `f89877b1…`, and each is recorded in the immutability manifest, which is itself sha-pinned (`test_hosted_v1_v4_immutability_manifest.py:40`). Editing a pin fails the manifest test.
- **Leak test.** `test_hosted_ask_memory_schema_pin.py` mutates the live schema and asserts the v1–v4 bytes stay fixed while v5 and command-binding move.
- **Fingerprint.** `tool_surface_contract.json` and `pending_tool_surface_sha256` moved together (`5366…` → `7401…`). The registered surface `8197…` is untouched. The command-binding `contractDigest` moved only on its v4 surface entry.
- **Other tools.** Every command's `outputSchema` in the v5 and v4 descriptors admits `cli_ops.envelope(False, …)`, except frozen v4 `ask_memory`, which is expected. The PR's all-tools test covers the local surface. I found no follow-ups for the same bug.

## Findings

**Medium (design): a second pinning mechanism.** `src/exomem/hosted_plugins.py:1900-1924` adds a separate file-based pin, but `hosted_legacy_schemas.py` already pins per-profile published command fields (`LegacyCommandContract`: params, description, annotations, input_schema). Adding an `output_schema` field there would keep one source of historical truth, and it is re-derived from the committed descriptors by `test_hosted_legacy_profile_pin.py`.

**Low: the pin is render-only.** `_apply_ask_memory_pin` runs in `compatibility_manifest` only (`hosted_plugins.py:1934`). A running server on a v1–v4 profile now advertises the new, wider schema. That matches the ruling. The side effect: frozen v4's `schema_contract_sha256` (`60b5…`) no longer equals the live v4 contract digest (`2620…`, `contracts/hosted-agent-command-binding-v1.json:25`). Until now the two matched. The code comment at `hosted_plugins.py:1947` says nothing cross-checks them, and the command-binding header check (`server_hosted.py:588`) uses the live value, so nothing breaks. The PR description should state this divergence explicitly.

**Low: fields outside `outputSchema` can still move frozen descriptors.** The new pin covers only `ask_memory.outputSchema`. Any other live-only field is caught by the existing legacy pin and the promotion digests, not by this PR.

**Nit:** `plugins/hosted/ask-memory-output-schema.json` exists as four byte-identical copies. One shared file would do, unless per-candidate divergence is expected.

**Nit:** `tests/test_mcp_output_schema_conformance.py:70`: `checked >= 2` is a weak floor, and `json.dumps(checked)` is an odd assert message. Assert that `ask_memory` was among the checked tools.

## CI

The required CI gate is green, and all core and harness shards pass. **"Offline static validation (not release proof)" is cancelled, not failed.** The job started at 14:03:16, and every step inside it passed: provisioner tests (1665 passed), the trivy secret scan, and `openspec validate`. It was still in post-job cleanup when the job's `timeout-minutes: 30` (`.github/workflows/hosted-infrastructure.yml:82`) expired at 14:33:22. A cold cache miss explains the time: `hosted-validators-v1` saved about 279 MB. Nothing in this diff caused it. It matters because the dependent jobs ("Published release promotion proof", the governance drill) were skipped along with it. One re-run on the now-warm cache should clear it.
