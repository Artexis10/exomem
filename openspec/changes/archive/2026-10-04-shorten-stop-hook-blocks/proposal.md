## Why

A Stop-hook block prints its whole reason in the client's block box ("Blocked by hook" in
Codex, "Stop hook feedback" in Claude Code). The capture check sent about 2,000 bytes of
doctrine on a session's first fire and after every compaction, and every fire in MCP mode
led with a 230-byte preamble. The reader saw a wall of instructions for what is a nudge.
The doctrine already lives in the shipped engagement reference, which the hook only
repeated.

## What Changes

- Every capture fire sends the one short check; the full first-fire text is removed.
  The short check names the incident rules and ends with a pointer to the shipped
  engagement reference, a vault path `read_memory` opens, so an agent with only the MCP
  connection can read the full rules.
- The MCP preamble becomes one clause of 90 bytes or less (skip when Exomem is not
  connected or cannot capture; bootstrap first if the contract is absent).
- The episode ask is tightened and keeps the record call, the key and the do-nothing escape.
- Unchanged: when the hooks fire (landing gate, cadence, cooldowns), `stop_hook_active`,
  the retrieval reminder, and the compaction re-arm of the cooldown stamp.

## Impact

- `src/exomem/_hooks/exomem_capture_nudge.py` and its generated copies
  (`plugins/claude-code/hooks/`, `plugins/cloud/generated/`).
- `shrink-bootstrap` delta: the capture check no longer has a once-per-session full text.
- Capability: `agent-bootstrap-contract`.
