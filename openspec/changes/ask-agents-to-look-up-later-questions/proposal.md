## Why

Some clients show the agent neither the server instructions nor a hook's per-turn context. One example is claude.ai with the connector and skills. In such a client, the agent recalls once at the start of a conversation, and only if the skill loads. Recorded misses from 2026-11-24 and 2026-11-25 show two gaps:

- The agent did not load the skill for a practical question about a job under way at the user's home, so it made no memory call.
- After the first recall, the agent answered later new questions from general knowledge, although the user had a recorded view on them.

Replays in the skills-only client, on Sonnet, measured each lever. On the same build without these changes, the agent made no memory call for the job question in two of two replays, and looked up neither later turn in two of two. A sentence in the skill body alone did not move the later turns in two replays, and one in the `recall_context` description did not in one. A line in the first recall result alone did not move them in two replays. The body sentence together with the line moved the first later turn to a memory lookup in seven of seven replays; one of them read the note that the recall had mapped, instead of searching. The second later turn stays weak: one of three replays read the relevant note, and none searched. The description sentence made the agent recall for the job question in four of six replays.

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
