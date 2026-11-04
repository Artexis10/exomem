## 1. The compile signal

- [x] 1.1 Port `episode_capture.parse`, so that a recap body reads back into its fields, and the `window_days` and `acted` parts of the dispositions effect block.
- [x] 1.2 Add `episode_coverage.py`: an episode's revisions, the cited revisions through `source_coverage.evaluate` with the caller's `lookup`, and the open Decided lines by exact equality.
- [x] 1.3 Add the `uncompiled_episode` backlog family, default `off`, outside the default attention union, with the recap skip kept in `unprocessed_source`. Bump the due-state schema version.
- [x] 1.4 Add tests: the revision chain, the egress twin, the write delta against the full rebuild, the default disposition and attention, and clearance without unsupported action attribution.

## 2. The close step

- [x] 2.1 Rewrite `episode_compile.py` to report the ref to cite, the Decided count and the open count, with no link resolution and no vault walk. Report `unchecked` when the count cannot be read.
- [x] 2.2 State only the result in `outcome`, add the conditional `next_step` only when live policy permits capture, and never ask for a second record.
- [x] 2.3 Replace the compile-first sentences in the scaffold skill, the engagement reference, the Stop hook episode ask, the `episode_due` advisory and the `record_episode` tool description with record-then-cite.
- [x] 2.4 Test the real `record_episode` tool: the open count, the count after a citing page, the cold snapshot, the gated next step and the empty recap.

## 3. Derived artifacts

- [x] 3.1 Refresh the skill contract stamp, the Claude Code plugin copies, the MCP schema baseline with its fingerprint, `docs/capabilities.md` and the Cloud plugin build, and run their checks. Move both pending fingerprints in the ChatGPT contract to the new digest.

## 4. Real-model proof

- [ ] 4.1 Run the close step and fresh-session canonical recall on the real models with realistic and hostile sessions, including a tool-only client with no hook. Report canonical correctness, restraint, recap, citation and recall separately. Add each failure found as a permanent regression case. The orchestrator runs this task.

## 5. The session-close suite

- [x] 5.1 Replace `recap_must_link` with `recap_cited` and add the `--hook-turn` mode, with tests on hand-built run snapshots.
- [x] 5.2 Separate generation inputs from independent answer keys. Add fresh-session canonical recall and native blind judgments, with tests for interrupted capture, unavailable transport and content provenance.
