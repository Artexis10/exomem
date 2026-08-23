## Why

Hosted is sold as the same product the operator runs locally. It is not.

Self-hosted exposes 29 product commands. `hosted-alpha-agent-v1` — the profile pinned for the in-flight 0.57.2 promotion — exposes 13. The richest profile that exists, `hosted-alpha-agent-v3`, exposes 17.

Stated as a tool count that sounds like a preference. Stated in the product's own vocabulary it is a capability hole. `simple_action_catalog()` resolves the eight product actions against whatever the active surface exports, and marks an action unavailable when no route survives:

| action | local | hosted v1 | hosted v3 |
|---|---|---|---|
| `adopt` | available | **unavailable** | **unavailable** |
| `maintain` | available | **unavailable** | **unavailable** |
| `record` | available | **unavailable** | available |

A paying hosted customer cannot adopt an existing vault and cannot maintain their knowledge base. Exomem itself reports those actions as unavailable; this proposal is not introducing that judgement.

Two further facts shape the fix.

First, `product-command-surface` already requires that "no public capability that exists on REST or CLI is missing from MCP unless it is terminal-local setup/admin". Adoption and maintenance are neither terminal-local nor setup-only. The hosted subset is in tension with a shipped requirement.

Second, the consolidation this change wants already exists. `simple-command-surface` specifies eight actions — `ask`, `remember`, `capture`, `review`, `connect`, `adopt`, `maintain`, `record` — implemented as `_SIMPLE_ACTIONS` with `simple_action_catalog()` mapping each to a canonical route, alternate routes, and advanced tools. It is bound only to CLI-family surfaces: `product_invoke.py` exports it on a descriptor whose `surface` is `cli`. Agents get the unconsolidated 29; hosted agents get 13 of them.

So the consolidated, simpler surface was designed, specified, implemented, and then never given to the agents who would benefit most from it.

## What Changes

- Make the simple-action layer the **customer-facing agent surface**, exposed on `mcp` as well as CLI, so an agent meets eight intent-shaped tools instead of twenty-nine command-shaped ones. The eight actions route to the existing canonical product commands; no command logic is duplicated and no leaf is bypassed.
- Keep every product command reachable. Consolidation reduces what an agent must *choose between*, never what it can *do*. The action layer's `advanced` and alternate-route entries carry the long tail.
- Add `hosted-alpha-agent-v4`, defined not as a hand-maintained membership list but as **the complete product surface**, tier 2 included. Parity then holds by construction rather than by vigilance.
- Require that any command withheld from hosted records a technical reason and the condition that lifts it. `transfer_artifact` and `adopt_vault` are the only two that qualify today, and only until their gateway flows are bridged.
- Leave `hosted-alpha-agent-v1`, `v2`, and `v3` membership, order, packages, locks, and recorded promotion evidence byte-identical. A published profile is never mutated.
- Render v4 for **both** platforms. v3 was deliberately Claude-only, which is why ChatGPT is furthest behind.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `simple-command-surface`: the eight product actions become available on agent surfaces, not only CLI-family ones, and the action catalog becomes the recommended agent entry point.
- `hosted-agent-surface`: gains `hosted-alpha-agent-v4`, and the standing rule that a hosted profile equals the complete product surface minus only commands carrying a recorded technical reason.
- `product-command-surface`: gains an explicit statement that the MCP-parity requirement binds hosted agent profiles, not just the REST and CLI surfaces.

## Impact

- Affected code: the action catalog and surface-profile registry in `src/exomem/commands.py`, the CLI-bound descriptor in `src/exomem/product_invoke.py`, tier-2 exposure for hosted in `src/exomem/server_hosted.py`, and the candidate registry in `src/exomem/hosted_plugins.py`.
- Affected artifacts: `src/exomem/tool_surface_contract.json`, `tests/fixtures/mcp_tool_schemas.json`, and new committed trees under `plugins/hosted/{candidates,generated/candidates,promotion/candidates}/hosted-alpha-agent-v4/` for `claude` and `openai`.
- Consumers: an agent that used a product command by name keeps working — legacy names remain resolvable for one release, the same way `_ADDITIONAL_CALLABLE_NAMES` still recognises the primitives that `PRODUCT_COMMANDS` itself folded away.
- Blocked on: substrate PR #144. `ask_memory` and `connect_memory` publish an `x-fastmcp-wrap-result` schema the gateway did not apply, so every hosted tool call returned `CELL_RESPONSE_INVALID`. No hosted tool call has ever succeeded; until that deploys, none of this is observable end to end.
- Promotion: command-surface membership is what `command_surface_sha256` digests, so this requires a new release and the 0.57.2 / v1 candidate is not the thing to promote. That is a deliberate trade, not a side effect.
- Explicitly out of scope: media (`process_media`, `read_media`) pending a media-capable hosted image and `worker_policy.media = true`; bridging `transfer_artifact` and `adopt_vault` to their gateway flows; and any change to the behaviour or schema of an existing command.
