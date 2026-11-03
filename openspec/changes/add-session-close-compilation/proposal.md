## Why

A session ends with the agent holding the most context about what changed. Today
the session recap is a log that points nowhere: nothing ties it to the pages that
the session changed. On 28 September an episode about one person never reached
that person's page, and a later answer served the stale page.

The first design asked a model to judge whether a page states each recap line. A
calibration on real recaps against a blind key showed that judgement does not beat
the base rate, so the server does not make it. The compiler, an agent, makes that
judgement. The server records only the fact that it can prove: a live compiled
page names the recap in its `sources:`.

The close step must work on every client. Tool-only clients, such as ChatGPT and
claude.ai, have no hooks, so the tool response is the main channel and the Stop
hook and the skill support it. `episode_memory` is also exposed on Exomem Cloud,
so a response change reaches Cloud.

## What Changes

- A recap is a Source with `ingested_into: []`. When a live compiled page cites it
  in `sources:`, source closure writes the back-reference and
  `source_coverage.evaluate` counts the recap as compiled. The new module
  `episode_coverage` reads that one signal over the revisions of an episode and
  names each Decided line that no cited revision holds.
- The close step is record, then cite. The agent records the recap once. Then it
  updates each page that a Decided item changes and cites the recap in the `sources:`
  of each compiled note updated; an entity, plan or record page takes no citation.
  The shipped skill, the engagement reference, the Stop hook episode
  ask, the `episode_due` advisory and the `record_episode` tool description carry
  this order. No text asks the agent to record again.
- The `record` response gets a `compile` block with the ref to cite, the Decided
  count and the open count. The `outcome` states that result only. A separate
  `next_step` rides only when live policy permits capture.
- A new backlog family `uncompiled_episode` holds one item per episode with an
  open Decided line. It ships `off`, stays out of the attention queue, and keeps
  recaps out of `unprocessed_source`.
- The dispositions view takes `window_days` (1 to 90, default 7) and reports
  `acted` for the family that can tell a citation from a re-read. Every other
  family reports `acted` as `unknown`. `surfaced` is stated as a lower bound.

## Impact

- Capabilities: `agent-bootstrap-contract`, `attention-queue`, `command-surface`.
- Code: `src/exomem/episode_coverage.py` and `episode_compile.py` (new),
  `episode_capture.py` (a recap body parses back into its fields),
  `episode_memory.py`, `audit.py`, `due_state.py`, `review_state.py`,
  `commands.py`, the Stop hook `src/exomem/_hooks/exomem_capture_nudge.py`,
  `episode_nudge.py`, the tool descriptions in `operation_entries.py` and the
  scaffold skill under `src/exomem/_scaffold/_Schema/`.
- Generated: the Claude Code plugin copies, the Cloud plugin packages,
  `docs/capabilities.md`, the tool schema fixtures and the tool surface contract.
- The due-state schema version rises from 3 to 4, so each install rebuilds its
  projection once.
- The Stop hook episode ask is 269 bytes and its MCP block at most 385 bytes. The
  tool surface fingerprint changes, so the ChatGPT connector must refresh before
  the new text reaches it. The total schema budget rises by 250 bytes.
- The `session-close` benchmark suite replaces its link check with `recap_cited`
  and gains a `--hook-turn` mode.
