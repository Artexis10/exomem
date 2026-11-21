## Context

`prominence.py` holds the capture doctrine that two carriers serve. Bootstrap serves it as the `balanced` and `maximal` capture text. The activation packet serves it again, every turn, as the `capture_policy` block, so a client that never reads the bootstrap still receives it. The hook renders that block under a cap of 1,600 characters. Above the cap it keeps only the block's first sentence, which is the live disposition and none of the capture rules.

The shared `_BASELINE_CAPTURE` sentence ends "tentative claims stay quiet". It sits next to the stable-preference classes, so it reads as a general rule. The `balanced` text also excludes "unresolved questions" in general. Together they teach an agent to leave unsaved any material that is not settled. The skill scaffold already allows a held view to be saved with explicit attribution and uncertainty.

## Goals / Non-Goals

**Goals**
- A held view, a question the work leaves open and the values a task settled are saved at a stepping stone, on every client.
- The served text and the scaffold say the same thing.

**Non-goals**
- No change to which personal baselines qualify, or to where they route.
- No new page type or vocabulary key. An open question is the existing `## Open Question` block inside a compiled page.
- No detector. The agent decides what is a held view, an open question or a settled value.

## Decisions

### Scope the quiet rule instead of removing it

Casual tentative claims about a person ("I might switch gyms") stay quiet, so the rule stays. The served text narrows it to "tentative events", the word the scaffold already uses. The words have the same length, so the bootstrap core grows by no bytes. The served baseline list therefore says "tentative events" (a same-length swap, because the core is at its ceiling), while the spec says "tentative personal claims". The baseline eligibility test already keeps an unsettled personal claim out.

### The full rule rides the activation block

The bootstrap core is at its byte ceiling. At `maximal`, the populated core had 6 to 41 bytes of headroom. The core therefore gets only the same-length wording fix, and `balanced` gets one clause (39 bytes) that replaces "or unresolved questions" with "a question the work leaves open is saved as an open question". `balanced` had 240 to 275 bytes of headroom before the clause and has 201 to 236 after it.

The full rule is one sentence in the activation `capture_policy` block, after the baseline doctrine. Every client receives that block, hook or MCP-only. A client that reads only the bootstrap still gets the `balanced` clause and the scaffold.

### The block has a hard budget

The longest rendering was 1,420 characters, at `silent` with `link_acceptance` `confirm-shortcut`. The new sentence is 176 characters and brings the longest to 1,598, so 2 characters remain. A longer sentence would push the block over the cap and the hook would silently keep only the disposition. The sentence is shortened to fit. The cap and the other constants stay as they are. One test renders the block at every non-`off` disposition and link-acceptance value and fails when the rendering lacks the sentence.

### An open question is not doubt about an event

A real-model probe showed the open-question class absorbing tentative events: a person unsure which day they posted a parcel had the doubt saved as an open question. A person's unsure report of an event or its details is a tentative event, and the existing lifecycle rule keeps it unwritten unless a collection offers a note field for it. A question the work itself leaves open, including whether something happened, stays an open question. The scaffold states both halves.

### The classes are stepping stones, and quiet material leaves no trace

A second probe, on fresh cases, found two failures. Sessions that ended on a held view or an open question, with nothing decided, saved nothing. And a served "not a tentative event" clause came back inside writes as a disclaimer ("do not record the unsure renewal"), which kept the excluded detail. The served sentence therefore names the three classes as stepping stones and names no excluded material: "Held views, open questions and settled values are stepping stones: save a view attributed, with its uncertainty; a question as open; values in full. Mention nothing kept quiet." The baseline sentence just before it still says that tentative events stay quiet, and the scaffold adds that no write mentions what stays quiet.

### The copyable instruction blocks stay within their caps

The copy-paste blocks in `docs/prominence.md`, `docs/ai-assistant-guide.md` and `QUICKSTART.md` carry the same quiet rule, and a hookless client pastes them first. A same-length swap to "tentative events stay quiet" aligns them. The `balanced` block also drops its "or open questions" exclusion, which only shortens it. Each block stays within the 1,500-byte cap; the `QUICKSTART.md` block is 1,489. The blocks do not carry the three classes: the blocks stay within their caps, and the bootstrap and the activation block carry the classes.

### Sources

- Anthropic, "The components of agent memory": memory keeps the facts and open items that later work needs, and not references to transient working state. This is the rule for settled values.
- Anthropic, "Effective context engineering for AI agents": always-on context stays minimal and high-signal. This is why the core gets a clause and the full rule goes to the per-turn block.

## Risks / Trade-offs

- The model may now save material that was mid-thought. The classes say "held" and "left open" and keep the exclusion of exploration and tangents. The real-model probe includes a control for that.
- Measured on fresh blind cases, the served text cuts saves of material that must stay unwritten sharply, while an open question about whether something happened is saved less often than before: the served sentence has no room for that tie-breaker. The next change moves the full predicate out of the capped block and restores it.
- The activation block has 2 characters left. The next addition must shorten something deliberately.
