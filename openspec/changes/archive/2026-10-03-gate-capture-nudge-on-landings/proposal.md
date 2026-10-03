## Why

The Stop hook blocks a turn with a capture reminder whenever the reply is long
enough (300 characters at `balanced`), unless the turn already wrote to the
knowledge base. Measured on one machine over 14 days it fired 1,994 times, on
about 76% of user prompts, and only 20% of those turns went on to write
anything. Each fire forces another full-context model turn, so the cost is high
and mostly spent on Q&A, CI-watching and reading turns.

## What Changes

- Below the most aggressive prominence level (`maximal`), the per-turn capture
  reminder fires only when the turn contains a landing: a successful shell
  command that commits, pushes, merges, tags, or creates or merges a pull
  request or release. The detector is structural and language-agnostic, and
  reads both Claude and Codex transcript shapes.
- `maximal` keeps the reply-length gate unchanged.
- The no-write-this-turn condition, `stop_hook_active` self-disarm and cooldown
  stay. The episode ask is unchanged and still covers decisions made in
  conversation with no landing.

## Impact

- `src/exomem/_hooks/exomem_capture_nudge.py` and its two committed copies
  (`plugins/claude-code/hooks/`, `plugins/cloud/generated/claude/hooks/`),
  regenerated rather than hand-edited.
- Capability: `agent-bootstrap-contract`.
