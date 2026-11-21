## Why

A recorded two-arm evaluation of Exomem with real agents (invented tasks, a fresh vault) showed four places where the
product's own output or text led an agent to a wrong step. The root rule is that Exomem fixes a misuse first, and does not
close it as agent error.

- A `recall_context` result in `maps` mode carries eight empty arrays from the older `anchors` packet. An agent read
  `anchors: []` as a recall that found nothing, and passed over the map and the turn slice that held the answer.
- A `search_memory` page hit often carried a heading line or a stub of a few words as its excerpt (about half of the hits
  in the evaluation). The agent chose pages by those excerpts and opened the wrong ones.
- The capture guidance names "a narrow Entity facet", while the default vocabulary declares none. Agents passed a `facets`
  argument that no type accepts and had the write refused.
- The skill text says that content outside the managed Knowledge Base stays read-only. An agent applied it to ordinary
  workspace files that are not part of the vault, and refused to write them.

## What Changes

- A `maps` packet drops each of the anchors-mode arrays that it leaves empty. When the turn slice found leads and no
  next action is due, the packet leads with one sentence that names the leads, in the room the next action reserves.
- A page hit's excerpt takes the matched chunk only when the chunk holds prose beyond its heading lines. A heading or a stub
  gives way to the query-anchored excerpt of the same page.
- The capture guidance names "a declared Entity facet", so an agent uses a facet only when the contract lists one.
- The skill scaffold limits the read-only rule to vault content. Ordinary workspace files are not the vault, and the agent
  writes them with its own file tools. Every mirrored copy is regenerated.

No tool name, request schema or tool description changes, so the tool-surface fingerprint and the connector contract digest stay as they are.

## Impact

- Specs: `context-activation` (the maps packet reply) and `find-recall-efficiency` (the page-hit excerpt).
- Code: `context_views.py`, `read_next_action.py`, `commands.py`, `find_results.py`, `prominence.py`.
- Text: the skill scaffold, the plugin and cloud copies built from it, and the capture guidance in the docs.
- Proof: scoped tests for each change; the effect on agent behaviour is measured on real models after merge, one change at a time, before any claim of improvement.
