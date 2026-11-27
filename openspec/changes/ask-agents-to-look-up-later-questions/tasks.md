## 1. Later-turns move

- [x] 1.1 Add `later_turns` to an MCP agent's first recall result, within the packet budget, with the reservation capped at a tenth of the requested characters.
- [x] 1.2 Test: an MCP agent's first recall carries it with map items; a recall with a `continuity` token, a non-MCP fetch, a coding surface, `off`, `light` and a surface that hides `search_memory` carry none; a small requested packet keeps its map.

## 2. Skill description

- [x] 2.1 Replace the description's immutability sentence, which the body already holds, with the practical-question sentence; regenerate the plugin copies and skill-contract stamps.

## 3. Proof

- [x] 3.1 Replay the incident cases on real models in the skills-only client against the same build without the change: the whole set once, the three failing cases three times, eight paired fresh cases, four general-question cases, the engagement-off cases, the skill body sentence alone on the `claude-ai` surface, and two cases that open on the user's own matter and then ask general questions.

## 4. Closure

- [ ] 4.1 Synchronize and archive after merge, with the strict gate before and after.
