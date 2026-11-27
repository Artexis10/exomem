## Context

Hooks render the activation `capture_policy` block's `rules` only up to `_CAPTURE_RULES_MAX_CHARS` (1,000), and drop it whole above that. Hooks that are already installed keep that bound, so the server must never serve a longer `rules`. The native MCP activation nudge has a byte ceiling, because it is sent on every substantive turn. The server instructions have a 900-character bound, because some clients cut them off.

## Decisions

- **Keep `rules` under 1,000 by rewriting, not by raising the bound.** An installed hook with the old bound would drop a longer rule whole, so raising the bound would silence the rule on every client that has not updated.
- **State the rule in the nudge itself.** A sentence that says "follow the capture rule" fails after a compaction, the very condition in which it is needed. A full sentence costs 253 bytes per turn and no model call. Re-serving the full rule on the compaction start event was not chosen: a long session also loses the rule without a compaction, and the per-turn sentence was measured.
- **Leave the server instructions alone.** At 898 characters they have no room. Any room would come from cutting clauses that earlier rulings chose to keep. In the real-model runs, clients without hooks made no memory call at all in operational sessions, so they are out of scope here; a follow-up change covers them.
- **No new Stop-hook trigger.** A reminder delivered on the next turn lands after the moment when the save was needed. A blocking reminder on every conversation turn forces an extra model turn. The text change is measured first.
- **The baseline text does not change.** `_BASELINE_CAPTURE` is shared word for word with the bootstrap core, which has 6 bytes of headroom at `maximal`. The new class in `rules` states that a relationship the person names is saved. The baseline text changes only if the measurement shows that the class does not reach the agent.

## Risks

- **Over-capture.** The agent more often saves nearby task detail along with the statement: in the held-out run, positives free of it fell from 12/24 to 9/24. The controls measure only wrong saves of a whole statement, and those did not rise. The follow-up change takes up the extra detail.
- **Per-turn cost.** The nudge grows by 253 bytes, about 60 tokens, on every substantive native MCP turn.
