# Design

## Context

Task discovery serves pages as leads. A page's semantic units reach the agent only when the agent reads the page.
A long page puts its late units behind every earlier section. Context engineering guidance names exactly this risk: give the model the minimum high-signal context it needs, and keep larger material behind references (Anthropic, "Effective context engineering for AI agents", 2025).
The person's recorded positions are high-signal. They belong in the packet itself, not behind a page read.

The design rests on replays of the failing conversation with a real model. Those replays were small and scanned by text, not judged, so they show mechanisms, not rates; the blind held-out run is the measurement. That follows the practice of diagnosing one production failure from its trace and keeping the failure as a regression case (Langfuse, "Evaluating LLM applications"; Anthropic, "Demystifying evals for AI agents", 2026).

## Decisions

### Select by a declared trait, not by relevance alone or by names

A recorded position often shares few words with the turn that needs it.
In the incident's replay, a question about a new measurement had to surface an attribution unit written weeks earlier. That unit shared almost no terms with the question, and a follow-up such as "why do you read it that way?" shares none.
Relevance ranking alone would miss it.
Selecting by category name would put vocabulary in code.

The registry already gives kinds a closed `traits` set that code branches on. This change adds the trait `position` and lets categories declare traits too.
A vault declares its own position categories through the existing reviewed save. The connected agent can propose those declarations from the category profile in `schema_memory(subject="categories")`. The agent is the sensor, and the owner confirms.

The shipped pack declares `position` only on `preference`. The shipped `constraint` and `decision` categories also cover technical limits and project choices, so declaring them would flood the block with units that are not the person's position.
A vault whose own positions sit under another core category, such as `insight`, adds the trait there with an entry that carries only `traits`; the core definition, aliases and description stay code-owned.

### Reach one hop from the served leads

The incident's position sat on a page that the served lead linked to, not on the lead itself.
The projection reads the served leads and the pages their served connections name, in either direction, which task discovery already resolves for the caller. The agent sees the same connections, so the block reads no page the packet does not already connect. That gives one batched semantic-unit query, restricted to those parent pages, with the trait's categories as its filter.
The query only names the pages that hold a position. The shared session-unit admission then reads those pages' own bytes, as the stance and the domain profiles do, so page lifecycle, supersession, expiry and egress decide each unit the same way for all three blocks.
A deeper walk would multiply cost for a case not yet observed.

### Rank within reach by relevance, then recency

Within the reached pages, every current position unit is ranked, and the bound applies after ranking. Units that share more words with the turn come first. Among equals, the newer unit comes first, so a later withdrawal or correction sits above the position it revises; on one date the later line on the page comes first.

### Own bound, outside the turn allowance

A block served on many turns must not draw on `max_chars`. An earlier block did, and task leads fell from 25 to 16 on a 23-turn replay until the block got its own bound.
`positions_max_chars` is a vault budget, pinned to the engineering bounds that the hook reads. The overflow references count against it after the lines, and 0 turns the block off.

### Serve a position whole when it fits

A careful causal attribution runs far past the working set's per-unit cap, and in the replays exactly those positions became references the agent never read. The block's own bound is therefore the only size test: a unit is served whole when its line fits, and otherwise becomes a reference, never a cut unit. The shipped bound leaves room for one long position and a short one.

### Read first, framing first

An earlier round of replays found the block used more often when it was the last field of the maps response than mid-packet, where it was mostly passed over. A later round on the installed service showed that last is not enough: the block was served and the first answer still led with a default explanation.

Dev replays of two incident conversations, with the served packet varied, then measured placement and wording separately.
- With a leading yes/no question that presupposes a default cause, and the earlier framing, every answer opened "Yes" and half left the position out.
- Placement moved the result. With the block first in the response, every first answer led with the position, at offset 0 in 4 of 4 replays, with no "Yes" before it. With the block last, 1 of 8 did.
- The framing's wording completed it: open the first answer with the position, attributed and dated; keep the observation apart from the cause attributed to it; do not answer inside a premise the position disputes; rank no other explanation above it without naming the evidence; state the position before any yes or no.
- A control with no recorded position showed no invented position.

So terminal egress moves the block to the front once every other field, including the served-context fields, is in place, and the hook renders it before the map and the task leads. The block still opens with `about`. A test pins the order because it is the measured mechanism, not markup, and a comment at the move records both results so that nobody moves it back. `budget.serialized_chars` is the length of the response as sent, and key order does not change it.

These are dev replays on one model and two conversations, not a held-out result. A task keeps the held-out real-model round open.

### Framing is the mechanism

The replays separated surfacing from presentation.
Raw units appended to the recall result changed nothing.
The same units, framed as the person's recorded positions, did change the replies, and no reply simply agreed with the position. The framing says to carry each position with attribution, to judge the evidence independently, and that each position stays theirs for the conversation.
The framing passage is therefore part of the contract, not decoration. It never asks for agreement, because agreement on command would turn a qualified hypothesis into an asserted fact.
Under a framing that asked the agent to attribute each line as its text does, the replies, in these replays, did not carry the person's positions. The framing therefore names the units as the person's own, and capture keeps the agent's own analysis out of position categories. The framing asks for engagement, never agreement: it asks the agent to state the position and then to judge the evidence, and it says to rank no other explanation above the position without naming the evidence that ranks it.

## Measured

One blind held-out run measured the change. It held 9 unseen cases: 6 positive cases and 3 controls, in which the saved position is contradicted, withdrawn or out of the trait's reach. Two arms ran, the behaviour before this change and this change, with 2 repeats each. A judge scored every reply blind against a reference written for its case. The judge came from the same model family as the agent under test and scored both arms the same way. Each figure below is the mean per-case difference, this change minus before, with its 95% confidence interval.

- **Position carried.** Final replies preserved the person's persisted position and attributed it to them in about two thirds more of the positive cases: +0.67 (0.33 to 0.92). Counting either reply, the gain is +0.42 (0.08 to 0.75): without the block, the agent often stated the position in its first reply and dropped it once the person questioned the reading.
- **Final-turn correctness.** It improved: +0.39 (0.17 to 0.61).
- **No automatic agreement.** It held within its preset bound: −0.06 (−0.17 to 0).
- **No invented facts.** It did not clear its preset bound; its interval is −0.28 to 0. The change's replies had two isolated slips against none before: an arithmetic error, and a write to a note that the case marked as one that must not change. The reference for this criterion was grounded in the seed notes.

## Risks

- **Few position units.** A vault with few declared categories serves an empty block. The capture guidance and the agent-proposed declarations address that, and an empty block costs nothing.
- **Agreement drift.** Framing could push the agent toward the person's view. The held-out set includes controls in which the saved position is wrong, withdrawn or contradicted by an outside fact. The decision run fails the change if those controls regress.
- **Self-echo.** This stays a residual risk. An agent's own conclusion misfiled under a position category would be presented as the person's view, and the agent would then agree with itself. Three things mitigate it: the capture step records the agent's own analysis under a category without the trait, the owner chooses which categories carry the trait and avoids ones that agents mostly write, and the held-out controls for agreement drift catch a regression.
- **Excerpt-level callers.** A caller served a page only as an excerpt gets that page's units up to the excerpt bound that page reads use, in total. That caller cannot read a unit by reference, so the units left out are counted, never named, and a unit on a page whose text bound is 0 is dropped. Terminal egress decides the bounds again, so a policy that tightens mid-request yields an unavailable block. The stance and domain profile blocks still serve such a caller a whole long unit; they remain as old debt.
- **Latency.** The change adds one query. The latency task proves its cost before release.

## Alternatives considered

- **Return a unit index first on long page reads.** This helps an agent that reads the page, but in the replays agents seldom read the long page.
- **A domain profile line per subject.** A profile line is the owner's confirmed standing instruction, which the agent applies. A position is a dated view, which the agent weighs. One framing for both would make the agent second-guess a rule, or apply an opinion as a rule. So profiles keep their own trait-free selection and framing.

## Coordination

- A page that supplies the stance or a domain profile is never read for positions, even on a turn where its own block does not serve it. Its units are standing lines with their own framing, so no unit arrives twice and no standing line arrives framed as a dated view.
- Unit admission, currency and egress pinning for session blocks live in the shared `session_units` module, which the positions block reuses rather than adding a third copy.
- The populated bootstrap core is near its hard ceiling, so this change adds no bootstrap text. The scaffold skill carries the reading guidance.
