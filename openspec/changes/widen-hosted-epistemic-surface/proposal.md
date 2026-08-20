## Why

Both shipped Hosted profiles are accumulation-only. `hosted-alpha-agent-v1` exposes thirteen commands and `hosted-alpha-agent-v2` adds `record_memory`; neither exposes `replace_memory`, `plan_memory`, or `edit_memory`. A Hosted user therefore cannot supersede a conclusion (no belief revision), cannot state an intent before acting (no planning), and cannot correct a page in place (every correction becomes a second, competing page).

That gap is worst exactly where it hurts most. The Hosted tier is hookless and maximally prominent, so it depends more on agent discipline than the local tier does, yet it is the tier structurally limited to pure accumulation — the anti-pattern the product's own governance thesis criticizes. The safety machinery that would make revision safe already exists and is already exercised by the commands themselves: `relation_review.py` refuses a `replacement` commit that is not bound to a predecessor page and content hash, and refuses an unreviewed relation disposition unless the caller re-runs validation and echoes the fresh `draft_hash`. Nothing about that guardrail is surface-specific, so withholding supersession from Hosted buys no safety.

## What Changes

- Add an immutable `hosted-alpha-agent-v3` profile to the canonical surface-profile registry: the full `hosted-alpha-agent-v2` membership in its pinned order, then `replace_memory`, `plan_memory`, and `edit_memory`.
- Leave `hosted-alpha-agent-v1` and `hosted-alpha-agent-v2` membership, order, packages, locks, and recorded promotion evidence byte-identical.
- Add a `hosted-alpha-agent-v3` client-plugin candidate with its own definition, records selection cases, and skills, including a new supersession/planning skill that teaches revision, intent, and in-place correction.
- Render and commit the v3 candidate for the Claude platform only. The OpenAI/ChatGPT channel stays on v1/v2 until a ChatGPT action-schema refresh is separately accepted.
- Generalize the Hosted candidate plumbing from a hard-coded two-candidate special case to a candidate registry, so the records reader floor, selection-case binding, and promotion evidence apply to every records-bearing candidate rather than to `hosted-alpha-agent-v2` by name.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-agent-surface`: gains the versioned `hosted-alpha-agent-v3` profile and the rule that a widened profile is additive to, and never a mutation of, an already-published profile.
- `hosted-gateway-contract`: gains the requirement that profile-widened mutation commands keep the cell's registry-derived proposal-first and mutation-boundary guarantees rather than acquiring gateway-side ones.

## Impact

- Affected code: the product surface-profile registry in `src/exomem/commands.py`, Hosted candidate plumbing in `src/exomem/hosted_plugins.py`, the `scripts/hosted-plugin.py` candidate choices, and new committed artifacts under `plugins/hosted/candidates/hosted-alpha-agent-v3/`, `plugins/hosted/generated/candidates/hosted-alpha-agent-v3/`, and `plugins/hosted/promotion/candidates/hosted-alpha-agent-v3/`.
- Consumers: a control plane may pin `hosted-alpha-agent-v3` for a cell that wants the complete epistemic loop. Cells stay on their configured profile until an operator changes it; the committed deployment lock still pins `hosted-alpha-agent-v2` as the active profile.
- Compatibility: profile membership is not the local tool surface. `src/exomem/tool_surface_contract.json`, `tests/fixtures/mcp_tool_schemas.json`, the v1 release-identity fixture, both v1 and v2 generated trees, and `deploy/chatgpt/personal-plugin-contract.json` are unchanged by this change.
- Explicitly out of scope: promoting v3, rendering an OpenAI package for v3, changing the deployment lock's active profile, and any change to `edit_memory`, `replace_memory`, or `plan_memory` behaviour or schemas.
