## Context

`prominence.py` holds the capture doctrine that two carriers serve. Bootstrap serves it as the `balanced` and `maximal` capture text. The activation packet serves it again, every turn, as the `capture_policy` block, so a client that never reads the bootstrap still receives it. The hook renders that block under a cap of 1,600 characters. Above the cap it keeps only the block's first sentence, which is the live disposition and none of the capture rules.

The shared `_BASELINE_CAPTURE` sentence ends "tentative claims stay quiet". It sits next to the stable-preference classes, so it reads as a general rule. The `balanced` text also excludes "unresolved questions" in general. Together they teach an agent to leave unsaved any material that is not settled. The skill scaffold already says the opposite: "Durable interpretations may be preserved with explicit attribution and uncertainty."

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

Casual tentative claims about a person ("I might switch gyms") stay quiet, so the rule stays. The served text narrows it to "tentative events", the word the scaffold already uses. The words have the same length, so the bootstrap core grows by no bytes.

### The full rule rides the activation block

The bootstrap core is at its byte ceiling. At `maximal`, the populated core had 6 to 41 bytes of headroom. The core therefore gets only the same-length wording fix, and `balanced` gets one clause (27 bytes) that replaces "or unresolved questions" with "a question the work leaves open is saved as open". `balanced` had 240 to 275 bytes of headroom before the clause and has 213 to 248 after it.

The full rule is one sentence in the activation `capture_policy` block, after the baseline doctrine. Every client receives that block, hook or MCP-only. A client that reads only the bootstrap still gets the `balanced` clause and the scaffold.

### The block has a hard budget

The longest rendering was 1,420 characters, at `silent` with `link_acceptance` `confirm-shortcut`. The new sentence is 161 characters and brings the longest to 1,582, so 18 characters remain. A longer sentence would push the block over the cap and the hook would silently keep only the disposition. The sentence is shortened to fit. The cap and the other constants stay as they are. One test renders the block at every non-`off` disposition and link-acceptance value and fails when the rendering is empty or lacks the sentence.

### Sources

- Anthropic, "The components of agent memory": memory keeps the facts and open items that later work needs, and not references to transient working state. This is the rule for settled values.
- Anthropic, "Effective context engineering for AI agents": always-on context stays minimal and high-signal. This is why the core gets a clause and the full rule goes to the per-turn block.

## Risks / Trade-offs

- The model may now save material that was mid-thought. The classes say "held" and "left open" and keep the exclusion of exploration and tangents. The real-model probe includes a control for that.
- The activation block has 18 characters left. The next addition must shorten something deliberately.
- The pasted instruction blocks in the public docs keep their old "tentative claims" wording. They are copy-paste text with a 1,500-byte cap, and a hookless client reads the bootstrap after pasting them. Aligning them is follow-up work.
