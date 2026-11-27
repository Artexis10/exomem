## Why

An agent working on an operational task often ignores durable things the person says in passing. On 2026-11-27 a person coordinating agent sessions stated a product vision, a priority and facts about two people they know. The agent saved none of it until the person asked. Three gaps caused this:

- After a compaction, the agent no longer held the capture rule. The full rule reaches a native MCP client only through `recall_context`, and the agent did not call it again.
- The per-turn activation nudge says when to recall, but nothing about when to save.
- The capture rule said "a remark made in passing is not a held view". Nothing in it said that what a person tells you about themselves counts, whatever the task.

## What Changes

- The capture rule gains one class: what the person tells you about their aims, priorities, the people they know, their belongings and their past is saved in the turn they say it, even as an aside, whatever the session's task, at its true strength and under `proactive_capture`. Trivia, one-offs and momentary reactions still get no note, and personal-baseline eligibility is unchanged.
- The rule drops "a remark made in passing is not a held view", because agents read it as "skip what is said in passing". A momentary reaction replaces it as the excluded case.
- The served rule keeps its 1,000-character bound. To make room, three other sentences are shortened without a change of meaning.
- The native MCP activation nudge, which the hook sends on every turn, gains one sentence that states this class itself. After a compaction the agent may no longer hold the rule that a recall served, so the sentence must not only point at that rule. The sentence goes out at `balanced` and `maximal`; `light` writes only on request, so it leaves the sentence out.
- The server instructions do not change, because they keep their 900-character bound. A client without hooks gets the class from every `recall_context` capture policy.
- Clients without hooks are out of scope. In real-model runs, an agent without hooks never loaded the skill and never called a memory tool in an operational session, on main or on this change. A follow-up change covers those clients, with its proof on real claude.ai and ChatGPT sessions.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-bootstrap-contract`: the capture predicate covers what the person says about themselves, whatever the task, and the native MCP activation nudge states that rule on every turn.

## Impact

- `src/exomem/prominence.py`: `_CAPTURE_RULES` is rewritten. The served `rules` is 997 characters against the bound of 1,000.
- `src/exomem/_hooks/exomem_retrieve_nudge.py` and its generated plugin copies: a new `_MCP_CAPTURE` sentence of 253 characters. The nudge grows from 1,445 to 1,698 bytes on every substantive native MCP turn, and its ceiling in `tests/test_nudge_diet.py` rises to 1,700.
- The scaffold capture guidance is aligned through its generators.
- No tool schema, write path or stored data changes. The behaviour depends on a model's judgement, so a real-model run was registered to decide the release. On 18 held-out cases with 2 repeats, right saves rose from 17/24 to 23/24 (paired +0.250, 95% CI [0.000, +0.542]), which misses the registered bar of a lower bound above 0. Wrong control saves fell from 2/12 to 1/12. The maintainer chose to ship on this result.
