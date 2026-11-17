# Proposal

## Why

People record their own positions in their notes: a working attribution of cause, an interpretation they prefer, a constraint on how something should be framed.
Such a unit often sits late in a long page. It is dated and qualified, for example "his working hypothesis, not proven".
When a later turn brings new evidence on the same subject, the agent should keep that position in view, attributed to the person, and judge the evidence against it.

In a dated incident in November 2026 it did neither, twice in one conversation.
Recall served a short linked page as the lead and never the long page that held the position. The agent answered in its default framing.
After the agent had read the long page in full, it still answered a challenge without the position.
The person had to restate a position the vault already held.

A replay with a real model reproduced the gap. The replay judged replies blind, and its findings were:
- Appending the raw units to the recall result did not change the replies.
- Presenting the same units as the person's recorded positions did change them. Labelled, the replies carried the position, attributed to the person, in 5 of 6 first replies and 3 of 6 challenged replies. Unlabelled arms carried it in none of 9.
- No labelled reply simply agreed with the position.

## What Changes

- Add a closed trait `position` to the kind-trait set, and let a category declare traits the way a kind does. A reviewed registry save declares which categories and kinds record positions. Code selects position units only through the trait, never by a category name.
- Serve a turn-scoped `positions` block in maps responses. It holds the position units found on the served task leads and on the pages those leads link to, ranked by relevance to the turn and then by recency. The block has its own bound outside the turn allowance.
- Frame the block as the person's recorded positions. The agent carries a position with attribution where the turn bears on it, judges the evidence independently, and says plainly where the evidence does not support the position or contradicts it. A recorded position stays the person's for the rest of the conversation. The framing never asks the agent to agree.
- Teach capture to record a person's position under a category that carries the trait.

## Capabilities

### New Capabilities

- `recorded-positions`: selection, projection and framing of the person's recorded positions for a turn.

### Modified Capabilities

- `semantic-unit-language`: categories can carry registry-declared traits from the closed trait set.

## Impact

The projection reuses task discovery's served leads and their listed links, and the semantic-unit store's batched query restricted to parent pages. It adds one query to recall, bounded by the number of served leads and their links, and its cost is proven on the recall latency instrument.
The trait reuses the registry's reviewed-save path. A vault declares its own position categories without a code release.
Domain profiles (`add-domain-profiles-and-curation`) can use the same trait to detect new preference or constraint units. This change adds no second mechanism for that.
