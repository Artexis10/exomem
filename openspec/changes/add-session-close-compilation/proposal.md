## Why

A session ends with the agent holding the most context about what changed.
Today the session recap is a log that points nowhere: real recaps carry no
`[[...]]` links, and their `about` refs live only in the recorder's private
ledger. On 28 September an episode about one person never reached that
person's page, and a later answer served the stale page.

The close step must work on every client. Tool-only clients, such as ChatGPT
and claude.ai, have no hooks, so the tool response is the main channel and the
Stop hook and the skill support it. `episode_memory` is also exposed on Exomem
Cloud, so a response change reaches Cloud.

## What Changes

- The close step has two parts. First the agent updates each page whose
  knowledge the session changed (entity facets, decisions, workflow contracts)
  with the ordinary writers. Then it records the recap, links each Decided item
  to its page with `[[...]]`, and sets `about` to the pages it updated.
- The shipped skill, the engagement reference, the Stop hook episode ask and
  the `episode_due` advisory all carry this order, in one short line where a
  byte ceiling applies. The Stop hook ask makes its page step conditional on live
  policy. A recap request alone does not authorize page edits at `off`.
- The `episode_memory` tool description asks the agent to link each Decided item
  to its page and points to the response's `compile` block.
- The `record` response gets a `compile` block. It lists the pages the recap
  names, each with its update time, counts the Decided items that name no page,
  and leads with one sentence that states that result. Only when live policy
  permits capture, a separate conditional `next_step` follows. Nothing asks the
  agent to record the recap again.
- The block names no page the caller cannot see and never counts one. A
  withheld page looks exactly like an absent one.

## Impact

- Capability: `agent-bootstrap-contract`.
- Code: `src/exomem/episode_compile.py` (new), `episode_memory.py`,
  `episode_capture.py`, `mutation_terminal.py`, the Stop hook
  `src/exomem/_hooks/exomem_capture_nudge.py`, `episode_nudge.py`, the tool
  descriptions in `operation_entries.py` and `commands.py` and the scaffold
  skill under `src/exomem/_scaffold/_Schema/`.
- Generated: the Claude Code plugin copies, the Cloud plugin packages,
  `docs/capabilities.md` and the tool surface contract.
- The Stop hook episode ask rises from 189 to 267 bytes and its MCP block from
  at most 310 to at most 385 bytes. The ceilings in `tests/test_nudge_diet.py`
  rise to match.
- The MCP `record_episode` description and its `decided` parameter grow by about 100
  bytes. The tool surface fingerprint changes, so the ChatGPT connector must
  refresh before the new text reaches it. The per-tool and total schema budgets
  rise by 100 bytes each.
