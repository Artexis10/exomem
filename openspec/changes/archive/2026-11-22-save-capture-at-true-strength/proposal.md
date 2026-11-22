## Why

Exomem tells agents to leave unsure material unwritten. A real-model run showed the result. A person gave an unsure report that later work needed, and the agent saved nothing or saved it as fact. Both are failures. The harm is to store a claim falsely, not to save it. An agent can save a claim in an accurate form: attributed, with its doubt.

The capture rule also does not reach every client. The hook renders at most 1,600 characters of the activation `capture_policy` block, and the longest block already fills 1,587 of them. A full rule cannot grow inside that text, and a hook installed earlier reads only `say`.

## What Changes

- One rule replaces the "stay quiet" lists: never write a claim more certain, more general or more settled than its source made it.
- An unsure report that later work could use is saved as unsure and attributed, with its doubt. It is never saved as a fact, an event or a baseline. A tentative claim never becomes a baseline: it gets no facet and no affiliation.
- A passing remark is not a held view. Trivia, one-offs and mid-thought exploration get no note, because nothing later needs them, not because they are uncertain.
- A write never mentions what stays unwritten.
- The activation `capture_policy` block carries the full rule under a new key, `rules`, at every disposition except `off`. `rules` has its own bound and takes no map room. The hook renders `rules` after `say` within its own cap. `say` keeps a one-sentence form of the rule for hooks that predate `rules`.
- The skill scaffold, the copyable instruction blocks and the served baseline text drop "tentative events" from the quiet list.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `durable-personal-baseline-capture`: a tentative claim never becomes a baseline, and it is saved as tentative when it has reuse value.
- `agent-bootstrap-contract`: the capture predicate states the true-strength rule and covers unsure reports.
- `context-activation`: the capture policy block carries the full rule under `rules`, which takes no map room, and its size bounds describe what the hook renders.
- `retrieve-inject-hook`: the hook renders `rules` after `say` on its own budget, and drops `rules` whole when it is over its cap.

## Impact

- `src/exomem/prominence.py`: one new constant, `_CAPTURE_RULES`, one new key in `capture_policy()`, a one-sentence `_EPISTEMIC_CAPTURE`, and a shorter `_BASELINE_CAPTURE` tail. The bootstrap core shrinks by 19 bytes at every level.
- `src/exomem/_hooks/exomem_retrieve_nudge.py` and its packaged copies: one new render budget, `_CAPTURE_RULES_MAX_CHARS`.
- The skill scaffold and the packages generated from it, and the copyable blocks in `docs/prominence.md`, `docs/ai-assistant-guide.md` and `QUICKSTART.md`.
- The block grows by the rule text, about 810 characters, and is sent once per conversation. The server does not count `rules` in `budget.used_chars` or in the room it reserves from the map, so the smallest `max_chars` that still receives the block is unchanged, and the first request's map is the one it was. `budget.serialized_chars` counts it. The hook's output grows by the same text, past its working-set ceiling.
- No tool schema, write path or stored data changes. The behaviour depends on a model's judgement, so a real-model run on the unsure-report, held-view, open-question and settled-value cases and on a trivia control decides the release.
