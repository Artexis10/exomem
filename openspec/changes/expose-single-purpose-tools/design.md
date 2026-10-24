## Context

Each grouped tool today is one Python leaf with one signature: a selector plus every field of every operation, all optional except the selector. `bind_vault` publishes that signature. The MCP middleware chain (`server.py:704-707`: refusal floor, public-name rewrite, authorization session, call trace) runs before any tool handler, and authorization classifies the raw tool name and arguments (`authorization_transport.py:738`). The binary-blob guard, the call ledger and the idempotency digest (`writer_lease.py:4202`) all key on the tool name and the top-level arguments as FastMCP fills them in with defaults.

`edit_note` already publishes one required `operation` object discriminated by `kind`, built from Pydantic models (`edit_operations.public_edit_operation_schema`). `record_memory.py` and `plan_memory.py` hold exact per-action field and required-field tables, which their leaves enforce.

## Goals

- Every operation's exact inputs are visible in the tool schema.
- Reads and writes are separate tools with honest annotations.
- No capability, guard, receipt or response shape changes.
- Old calls keep working; retries across spellings replay one write.
- The mechanism is reusable for the twelve remaining grouped tools.
- The pilot measures whether the new shape changes agent performance, and which part of it does.

## Non-goals

- Converting the other grouped tools (a later change).
- Changing `recall_context` inputs (a separate, measured change).
- Cloud support for operations that Cloud refuses today.

## Decisions

**D1. One read tool and one write tool per object, with a discriminated `operation`.** Each tool takes `operation: {kind, ...}`. A nested object, not a top-level `oneOf`: hosts already accept the nested `edit_note` shape, while the Anthropic API rejects a top-level union in `input_schema` and OpenAI strict mode needs a root object. One tool per operation (about 113 tools) was rejected for its count.

Pilot variants keep the existing action names as `kind` values, so guidance, errors and the selector registry share one vocabulary:

| Tool | Variants |
| --- | --- |
| `read_records` | `describe`, `inspect`, `validate`, `query`, `history`, `import` with `mode` `preview` or `status` |
| `change_records` | `create`, `append`, `update`, `bulk_upsert`, `discard`, `revise`, `rebaseline`, `import` with `mode` `start` or `cancel` |
| `read_plans` | `inspect`, `validate`, `query`, `history` |
| `change_plans` | `create`, `add`, `update`, `triage`, `revise`, `rebaseline` |

Coverage is keyed on (action, import mode), so each pair belongs to exactly one tool. Either/or argument rules (Records `validate` forms, `append` with or without a held candidate) stay in the leaf's validation.

**D2. A rendering is a public spelling of the grouped command, translated in middleware.** A rendering names its tool, its grouped command, its variant set and its annotation. `resolve_invocation(public_name, arguments) -> (command, flat_arguments)` is one pure function. A rendering-aware name rewrite runs where the public-name rewrite runs today, before authorization: it validates the operation against that rendering's own variants, flattens it, and hands FastMCP the grouped tool name with flat arguments while carrying the rendering name forward for tracing. FastMCP then dispatches to the hidden grouped tool. Authorization, the blob guard, defaults, the idempotency digest, egress `request_kwargs`, the ledger and receipts are those of a direct grouped call by construction. Transport-owned top-level keys pass through untouched. A rendering handler never calls the bound wrapper itself. Alternative rejected: four new commands, which would need new egress, lease, credential and receipt coverage for unchanged behaviour.

**D3. Validation happens before the lease and speaks the grouped command's language.** The translation step fully validates the variant, refuses a kind or import mode outside the rendering (naming the sibling tool when the kind belongs to it), and returns the grouped command's own refusal envelope (`INVALID_RECORD_ARGUMENTS` and the Planning equivalent) as a normal tool result. A refused call still writes a ledger row.

**D4. One field table per operation.** Operation entries hold each variant's fields, required fields, effect class and destructive flag. The Records and Planning leaves import their allowed and required tables from them. A startup assertion checks that every (action, mode) pair maps to exactly one variant and that every read variant is read-only under `invocation_is_read_only`.

**D5. Grouped names are hidden on MCP, and nothing on MCP tells an agent to call them.** Hosts such as Claude Code, ChatGPT and claude.ai let a model call only listed tools, so a hidden spelling is callable only by clients that already know it. Discovery hides a grouped command that has renderings. Every MCP-facing text and payload names the rendering: bootstrap contracts and routes, the action catalog, the surface descriptor, the `create_note` Records disposition payload, terminal-response validation, remediation strings, hook reminders and the scaffold skill. REST and CLI keep the grouped spelling. A test fails when any MCP response or shipped guidance names a hidden spelling.

**D6. The capture-nudge hook classifies by rendering.** `change_*` counts as a write and `read_*` does not, so a correctly filed record does not trigger the nudge.

**D7. Annotations.** Read tools: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`. Write tools: `readOnlyHint=false`, `destructiveHint=true` when any variant overwrites or removes. The hidden grouped spellings keep their current annotations.

**D8. The rendering shape is selectable so the eval can separate its causes.** The server renders converted objects as `nested` (default: D1), `flat` (the same read/write split, each tool with an `action` enum limited to its side and only that side's fields) or `grouped` (today's surface), from the same entries. The eval compares the three on one build. After the pilot, the losing shapes are removed.

**D9. The pilot is judged by agent performance first.** A paired tool-selection eval runs Records and Planning tasks on the three shapes with the same cases, model and host:
- Primary metric: task success, checked from the vault's end state against a key written from source truth, never from system output.
- Unit: the case, grouped by task template (at least six groups), compared with the sign-flip interval in `benchmarks/membench/utility/switch_eval/stats.py`.
- Margin: non-inferiority δ = 0.10, the house floor (`switch_eval/protocol.py`), raised toward 0.15 only if the dev round's measured spread and `power_step` require it. Pass when the lower bound of the per-case difference is above −δ; an interval that includes zero reads "no change".
- Size: a dev round of about 20 cases × 3 shapes × 2 repeats, then about 90–120 held-out cases × 2 repeats, generated and frozen after the dev round.
- Wrong writes: new-shape cases with a write during a read task, minus grouped-shape cases, at most 1, each one inspected.
- Secondary readouts: first correct operation on the resolved (command, action, mode), calls per task, argument refusals, schema bytes and input tokens per task.
- Isolation: each arm loads only its own rendering's guidance; skill digests and the host's tool-search setting are recorded.
The directory rescan after release is a secondary signal.

## Risks

- The directory may still flag a discriminated union. The fallback is manual review, then a per-variant rendering only for OpenAI clients from the same entries.
- Schema bytes grow: Records from about 3.6 KB to 8.2 KB, Planning from about 1.7 KB to 4.8 KB (offline estimate). The pilot measures them, and round 2's budget is set from that measurement.
- Planning is a structural twin of Records, so round 2's harder families (flag-conditional read/write, governance credentials) get offline `resolve_invocation` seam tests in this change before they convert.
