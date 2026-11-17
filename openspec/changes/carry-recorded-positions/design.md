# Design

## Context

Task discovery serves pages as leads. A page's semantic units reach the agent only when the agent reads the page.
A long page puts its late units behind every earlier section. Context engineering guidance names exactly this risk: give the model the minimum high-signal context it needs, and keep larger material behind references (Anthropic, "Effective context engineering for AI agents", 2025).
The person's recorded positions are high-signal. They belong in the packet itself, not behind a page read.

The design rests on replays of the failing conversation with a real model. Those replays reused one vault across repeats, so they show mechanisms, not rates; the blind held-out run decides. That follows the practice of diagnosing one production failure from its trace and keeping the failure as a regression case (Langfuse, "Evaluating LLM applications"; Anthropic, "Demystifying evals for AI agents", 2026).

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
The projection reads the served leads and their listed link targets, which task discovery already resolves for the caller. That gives one batched semantic-unit query, restricted to those parent pages, with the trait's categories as its filter.
The query only names the pages that hold a position. The shared session-unit admission then reads those pages' own bytes, as the stance and the domain profiles do, so page lifecycle, supersession, expiry and egress decide each unit the same way for all three blocks.
A deeper walk would multiply cost for a case not yet observed.

### Rank within reach by relevance, then recency

Within the reached pages, every current position unit is ranked, and the bound applies after ranking. Units that share more words with the turn come first. Among equals, the newer unit comes first, so a later withdrawal or correction sits above the position it revises; on one date the later line on the page comes first.

### Own bound, outside the turn allowance

A block served on many turns must not draw on `max_chars`. An earlier block did, and task leads fell from 25 to 16 on a 23-turn replay until the block got its own bound.
`positions_max_chars` is a vault budget, pinned to the engineering bounds that the hook reads. The overflow references count against it after the lines, and 0 turns the block off.

### Serve a position whole when it fits

A careful causal attribution runs far past the working set's per-unit cap, and in the replays exactly those positions became references the agent never read. The block's own bound is therefore the only size test: a unit is served whole when its line fits, and otherwise becomes a reference, never a cut unit. The shipped bound leaves room for one long position and a short one.

### Read last, framing first

In the replays the agent used the block far more often when the block was the last field of the maps response and its framing came before its items. Mid-packet, with the framing after the items, the block was mostly passed over. So terminal egress moves the block to the end once every other field, including the served-context fields, is in place, and the block opens with `about`. A test pins that order because it is the measured mechanism, not markup.

### Framing is the mechanism

The replays separated surfacing from presentation.
Raw units appended to the recall result changed nothing.
The same units under a framing passage did change the replies, and no reply simply agreed with the position. The framing says to judge the evidence independently and that a position the person holds stays theirs for the conversation.
The framing passage is therefore part of the contract, not decoration. It never asks for agreement, because agreement on command would turn a qualified hypothesis into an asserted fact.
It attributes each unit as the unit's text does, not wholesale to the person, because a position category can also hold a conclusion that an agent saved.

## Risks

- **Few position units.** A vault with few declared categories serves an empty block. The capture guidance and the agent-proposed declarations address that, and an empty block costs nothing.
- **Agreement drift.** Framing could push the agent toward the person's view. The held-out set includes controls in which the saved position is wrong, withdrawn or contradicted by an outside fact. The decision run fails the change if those controls regress.
- **Self-echo.** An agent's own conclusion, saved by capture under a position category, could come back next session as the person's view, and the agent would then agree with itself. Categories that agents mostly write, such as a core category a vault marks, widen that path. The framing attributes each unit as its text does, so an unnamed conclusion reads as one the notes record, and the capture guidance puts the agent's own analysis under a category without the trait and has a position name its holder.
- **Latency.** The change adds one query. The latency task proves its cost before release.

## Alternatives considered

- **Return a unit index first on long page reads.** This helps an agent that reads the page, but in the replays agents seldom read the long page.
- **A domain profile line per subject.** A profile line is the owner's confirmed standing instruction, which the agent applies. A position is a dated view, which the agent weighs. One framing for both would make the agent second-guess a rule, or apply an opinion as a rule. So profiles keep their own trait-free selection and framing.

## Coordination

- A page that supplies the stance or a domain profile is never read for positions, even on a turn where its own block does not serve it. Its units are standing lines with their own framing, so no unit arrives twice and no standing line arrives framed as a dated view.
- Unit admission, currency and egress pinning for session blocks live in the shared `session_units` module, which the positions block reuses rather than adding a third copy.
- The populated bootstrap core is near its hard ceiling, so this change adds no bootstrap text. The scaffold skill carries the reading guidance.
