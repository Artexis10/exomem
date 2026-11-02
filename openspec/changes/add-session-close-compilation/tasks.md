## 1. The close step in the contract text

- [x] 1.1 Replace the record-only step in the scaffold skill and the engagement reference with the two-part close step, kept generic.
- [x] 1.2 Add the compile-first sentences to the `record_episode` tool description, the Stop-hook episode ask and the `episode_due` advisory; raise the Stop-hook and tool-schema byte ceilings by the added words only.

## 2. The `compile` block

- [x] 2.1 Add `episode_compile.py`: resolve Decided-item links over the visible pages before the write, then build the block after the commit.
- [x] 2.2 Return the block from `episode_memory.record` and register it in the compact episode envelope fields.
- [x] 2.3 Add tests in `tests/test_episode_compile.py`: listing, egress twin, unlinked count, omitted block, failed link check and the compact envelope of a real MCP call.

## 3. Derived artifacts

- [x] 3.1 Refresh the skill contract stamp, the Claude Code plugin copies, the MCP schema baseline with its fingerprint, `docs/capabilities.md` and the Cloud plugin build, and run their checks. Move both pending fingerprints in the ChatGPT contract to the new digest.

## 4. Real-model proof

- [ ] 4.1 Run the close step on the real models with realistic and hostile sessions, including a tool-only client with no hook. Add each failure found as a permanent regression case. The orchestrator runs this task.
