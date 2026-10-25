## 1. Operation entries and renderings

- [x] 1.1 Add operation entries for Records and Planning: kind, rendering, fields, required fields, effect class and destructive flag, keyed on (action, import mode). Make `record_memory.py` and `plan_memory.py` import their allowed and required tables from them. Verify: the existing Records and Planning argument tests pass unchanged.
- [x] 1.2 Add renderings `read_records`, `change_records`, `read_plans`, `change_plans` and the pure `resolve_invocation(public_name, arguments)`. Verify: unit tests map every variant to the grouped command and flat arguments; an unknown name or kind fails closed.
- [x] 1.3 Translate renderings in middleware before authorization, then dispatch to the hidden grouped tool; carry the rendering name forward for tracing and record it in a content-free ledger field. Verify: for the same logical call, the authorization decision, blob guard, ledger row targets, egress request arguments, receipts and idempotency digest equal a direct grouped call; a retry across spellings with one key replays one write.
- [x] 1.4 Validate fully in the translation step: refuse a kind or import mode outside the rendering before any lease with the grouped refusal envelope, naming the sibling tool. Verify: `append` and `import` with mode `start`, missing or malformed sent to `read_records` are refused and the writer coordinator is never contacted; the refusal still writes a ledger row.
- [x] 1.5 Add the startup coverage assertion. Verify: removing one variant, duplicating a pair, or moving a write into a read tool fails startup.
- [x] 1.6 Hide grouped commands that have renderings from MCP discovery; keep them callable; set annotations per D7. Verify: `list_tools` shows the four renderings and neither grouped name; previously valid grouped calls return the same results; Cloud and tier exclusions hold for every spelling.
- [x] 1.7 Add the rendering-shape option (`nested` default, `flat`, `grouped`) from the same entries. Verify: each shape lists the expected tools and schemas, and all three resolve to the same command and arguments for the same logical call.
- [x] 1.8 Add offline `resolve_invocation` seam tests for `govern_memory` (argument-dependent credential rule) and one flag-conditional family (`maintain_memory` `dry_run`), without rendering them. Verify: the tests show where those families need rules before round 2.

## 2. MCP-facing guidance and generated surfaces

- [x] 2.1 Render every MCP-facing name from the active rendering: bootstrap Records and Planning contracts and routes, the action catalog, the surface descriptor's available and callable names, the `create_note` Records disposition payload and its terminal validation, remediation strings, hook reminder text and the scaffold skill. Keep REST and CLI on the grouped spelling. Verify: a test fails when any MCP response or shipped guidance names a hidden spelling; REST and CLI guidance is unchanged.
- [x] 2.2 Make the capture-nudge hook count `change_*` as a write and `read_*` as a read. Verify: a filed record through `change_records` does not trigger the nudge; the plugin copies match.
- [x] 2.3 Regenerate the schema fixtures, tool surface contract, capability docs, skill contract, both plugin trees and the pending ChatGPT digest; set byte ceilings for the four tools from their measured size. Verify: each generator's check and `tests/test_tool_schema_budget.py` pass.

## 3. Tool-selection eval

- [x] 3.1 Build the paired eval: Records and Planning tasks grouped by template (at least six groups), each with a vault seed and an end-state key written from source truth; three arms (`grouped`, `flat`, `nested`) on one build; `claude -p` stream-json against an isolated stdio server with only that arm's guidance loaded; skill digests and the tool-search setting recorded. Scoring: task success from vault end state (primary); first correct resolved (command, action, mode), calls per task, argument refusals, schema bytes and input tokens (secondary); writes during read tasks. Verify: offline tests with an injected runner score recorded transcripts.
- [x] 3.2 Run the dev round (about 20 cases × 3 arms × 2 repeats); set δ from `power_step` (floor 0.10, cap 0.15) and the held-out size; generate and freeze about 90–120 held-out cases. Record the cost before running.
- [x] 3.3 Run the held-out round once. Record each arm's per-case paired difference against `grouped` with its sign-flip interval, and the wrong-write counts, in this change. Verify: the chosen shape's lower bound is above −δ and its wrong-write excess is at most 1.

## 4. Delivery

- [x] 4.1 Remove the `flat` shape; keep `nested` as the only listed shape and `grouped` only as the eval baseline (D8).
- [x] 4.2 Run `openspec validate --all --strict`, the scoped suites and the full corpus; get an independent review; merge; cut the ordinary release. PR #2082 merged as `fb3a394bb`; closeout #2086 merged as `e711174c1`, each independently approved. Scheduled full CI `40173428484` passed at `e711174c1`; release PR #2078 merged as `616940db7`, and `v0.143.0` published on 2026-10-25. Runtime qualified the published Python artifacts and signed images.
- [x] 4.3 Make the Substrate gateway accept both spellings before Cloud cells list the renderings. Already true, so no gateway change: the gateway passes `tools/list` through and intercepts `tools/call` only for the artifact tools (`capture_source`, `preserve_artifacts`) and the Cloud transfer tools (`observe_transfer`, `transfer_memory`); it forwards every other call. It names neither the grouped nor the rendered Records and Planning tools (searched on 2026-10-25).
- [ ] 4.4 After the operator deploys Cloud and rescans, record the scan result for the four tools.
