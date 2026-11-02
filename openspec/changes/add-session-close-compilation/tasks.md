## 1. The close step in the contract text

- [ ] 1.1 Replace the record-only step in the scaffold skill and the engagement reference with the two-part close step, kept generic.
- [ ] 1.2 Add the compile-first sentences to the `episode_memory` tool description, the Stop-hook episode ask and the `episode_due` advisory; raise the Stop-hook byte ceilings by the added words only.

## 2. The `compile` block

- [ ] 2.1 Add `episode_compile.py`: resolve Decided-item links over the visible pages before the write, then build the block after the commit.
- [ ] 2.2 Return the block from `episode_memory.record` and register it in the compact episode envelope fields.
- [ ] 2.3 Add tests in `tests/test_episode_compile.py`: listing, egress twin, unlinked count, omitted block, failed link check and the compact envelope of a real MCP call.

## 3. Derived artifacts

- [ ] 3.1 Regenerate the Claude Code plugin copies, the hosted plugin render, the hosted v5 candidate, `docs/capabilities.md` and the Cloud plugin build, and run their checks.

## 4. Real-model proof

- [ ] 4.1 Run the close step on the real models with realistic and hostile sessions, including a tool-only client with no hook. Add each failure found as a permanent regression case. The orchestrator runs this task.
