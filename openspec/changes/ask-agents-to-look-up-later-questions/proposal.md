## Why

Some clients show the agent neither the server instructions nor a hook's per-turn context. One example is claude.ai with the connector and skills. In such a client, the agent recalls once at the start of a conversation, and only if the skill loads. Recorded misses from 2026-11-24 and 2026-11-25 show two gaps:

- The agent did not load the skill for a practical question about a job under way at the user's home, so it made no memory call.
- After the first recall, the agent answered later new questions from general knowledge, although the user had a recorded view on them.

Replays in the skills-only client measured each lever on Sonnet, with the server serving the `claude-ai` surface and semantic search on. Each count below comes from one configuration, against the same build without these changes:

- Without the changes, the job question got no memory call in two of two replays, and neither later turn got a lookup in two of two.
- With the description sentence, the job question got a memory call in two of three replays.
- With the body sentence alone, the first later turn got a lookup in one of three replays.
- With the recall-result line alone, it got none in one replay.
- With the body sentence and the line together, it got a memory call in four of four, and a new search in three of four. The fourth reread a page that an earlier search had found.
- The second later turn got no new lookup in any of the four replays. This lever does not fix that turn.

Earlier rounds on other builds agree: the description sentence moved the job question in two of three, and the line alone in an older wording missed once.

Neither change adds memory calls where none belong:

- Eight paired fresh cases: both builds pass all eight. Only two of them put a new question after the first recall, and the old build already passed both, so this set does not measure the gain.
- Four general-question cases: no memory call in any replay.
- Five engagement-off cases: no memory call in any replay.
- Two new cases that open on the user's own matter and then ask general questions: no lookup on a general later turn in four of four replays on each build, although the new build served the line in all four.

## What Changes

- An MCP agent's first `recall_context` of a conversation carries `later_turns`. It is one sentence that asks the agent to look up a later new question or claim on the user's life, work or a topic their memory lists. It says that a question about nothing of theirs needs none. It names the calls as the serving surface spells them.
- The sentence is served only in the conversation engagement context, at `balanced` or `maximal`, and only when the caller can make both calls. A recall with a `continuity` token gets none. A hook, CLI, REST or coding-surface fetch gets none.
- The packet holds back the sentence's characters before it composes the map, but only when they are at most a tenth of the requested characters. A smaller request keeps its map, and the sentence rides only on room left over.
- The skill body asks for a lookup on each later turn that brings a new question, claim or decision, even about the same person and in the same thread.
- The shipped skill's description says that a practical question about a job, purchase or booking under way for the user is their own case. The sentence it replaces, on immutable Sources and read-only vault content, stays in the skill body.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-prominence-control`: the first recall result asks a client without per-turn guidance for a lookup on each later new question, and the skill description counts a matter under way as the user's own case.

## Impact

- Code: `read_next_action.py`, `commands.py` (the `activate_context` path). Skill scaffold and generated plugin copies. The session-close bench's skills-only modes now serve the `claude-ai` surface.
- Behaviour: claude.ai agents make more `search_memory` calls on later turns that ask something new. An agent's first map can lose up to about 240 characters of items. Coding surfaces and engagement levels `off` and `light` see no change.
- Undo: revert the change. No stored data changes.
