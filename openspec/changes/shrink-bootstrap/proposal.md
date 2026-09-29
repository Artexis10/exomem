## Why

Every agent session on every client pays the compact bootstrap before its first real turn. Measured on this tree it is 63,055 bytes of served JSON at `maximal` (about 15,800 tokens), 62,698 at the default `balanced`, and 60,481 at `off`. The level barely matters: `off` is 96% of `maximal`, so a client that asked Exomem to stay quiet still pays for the whole operating manual.

The ceiling that guards it (`COMPACT_BYTE_CEILING = 63,300`) is not a budget. It was raised five times to fit whatever had been added, and its own commentary records each raise as "spent the whole growth budget". Nothing in the payload asks whether a block is needed on every session or only when a task arises. Most blocks are the latter: semantic-authoring syntax (9,042 B) matters when writing a compiled note, knowledge-pack catalogues (3,935 B) when adopting a vault, Records and Planning routing (4,196 B) when the user mentions those, the delegation envelope class table when a restructure is proposed. The same tool routing is stated five times (`simple_actions`, `front_door_actions`, `product_commands`, `tool_defaults`, `active_capabilities`) for 10,286 B, about 16% of the payload.

## What Changes

- Split the compact bootstrap into a small always-served **core** and named **sections** retrievable on demand. The core carries every rule that prevents a known incident (recall before answering, the capture loop, episode recording, withheld is indistinguishable from absent, supersede never overwrite, the delegation ceiling, the due-state restraint). Sections carry the reference detail, byte-identical to what compact serves today.
- Propose a core ceiling of 15,000 bytes at `maximal` on the worst-case surface, with a design allocation of about 13,900 bytes and a margin of about 1,100 bytes. That is roughly 78% smaller than today. `COMPACT_BYTE_CEILING` is replaced by a core ceiling derived from that allocation, plus a per-section ceiling so a single section cannot quietly regrow into the old payload.
- Retrieve sections through a new `section` argument on `bootstrap` (live and vault-derived blocks) and through `read_memory` of the shipped skill references (prose references), both existing read paths. No new tool.
- Apply the diet to every surface whose bootstrap contract is not published: the generic MCP surface, Claude Code, and `hosted-alpha-agent-v5`. The released hosted profiles `hosted-alpha-agent-v1` to `-v4` keep their payload byte for byte, pinned by golden digests, and keep their pinned `bootstrap` schema (which has no `section` argument).
- Prove no behaviour regression by a machine-checked rule manifest (every named core rule present at every level that must carry it), a losslessness check (core plus all sections reconstructs the pre-diet compact payload), the existing bootstrap, memory-loop and continuity suites, and a frozen-profile digest test.

Phase 1 (this change) contains the measurements, the proposal and the spec delta only. No code changes and no derived artifacts are regenerated until the ruling in `design.md` is given.

## Capabilities

### Modified Capabilities

- `agent-bootstrap-contract`: compact bootstrap becomes a core plus on-demand sections with a byte budget derived from the core allocation; a rule manifest and a frozen-profile pin are added.

## Impact

Code: `commands.op_bootstrap` and its projection helpers, `_filter_bootstrap_payload`, `prominence` contract text, the `bootstrap` command's parameter list (non-legacy profiles only), and the hosted v5 compatibility descriptor. Tests: `tests/test_bootstrap*.py` migrate from reading blocks off the compact payload to a helper that assembles core plus sections; new `tests/test_bootstrap_core_rules.py`, `tests/test_bootstrap_frozen_profiles.py`. Derived artifacts (tool schemas, plugin tree, hosted render, v5 candidate, `docs/capabilities.md`) regenerate in Phase 2 per `CONTRIBUTING.md`. Released hosted candidates v1 to v4 do not change. The skill scaffold under `src/exomem/_scaffold/_Schema/` stays generic and is not the diet's target: it is the retrieval source, not a cost.
