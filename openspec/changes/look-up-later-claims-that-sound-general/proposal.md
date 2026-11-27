# Look up later claims that sound general

## Why

The first recall's `later_turns` sentence asks an agent to look up a later turn's new question on a subject the user's memory lists, and it ended "One about nothing of theirs needs none." In replays of a three-question health conversation on the claude.ai skills-only client, the agent never searched before answering the third question, a claim about a village of long-lived people. That covers 10 recorded draws across every build. The agent judged the claim to be general knowledge and used that exit. The user's own rule for judging population claims sits in a note that only a search finds. The agent reached it only when an earlier search had happened to list that note.

## What Changes

- The `later_turns` sentence drops its exit clause. It now asks for a `search_memory` on a later new question or claim on a subject the memory lists, even when it sounds like general knowledge or comes from someone else, because the user's own view or rule shows only in a search.
- The sentence stays within its 240-character bound, so the packet budget is unchanged.

Replays on the same instrument (skills-only client, claude.ai surface, semantic search, Sonnet), against main:

| Check | Main | This change |
|---|---|---|
| New search before the third question | 0 of 3 | 3 of 3 |
| Extra calls on general follow-up turns after a personal first turn | 0 of 4 | 0 of 4 |

An unrelated home-object case still looks up on its personal turn on both builds.

## Capabilities

### Modified Capabilities

- `agent-prominence-control`: the later-turns sentence no longer tells the agent that a question it judges general needs no lookup.

## Impact

- Code: `src/exomem/read_next_action.py` (one sentence).
- Risk: more searches on later turns whose subject the memory lists. The general follow-up guards show none in four draws. A general question about a subject the memory does not list is outside the sentence.
- Not measured: ChatGPT and other hookless surfaces; hook surfaces do not get the sentence.
