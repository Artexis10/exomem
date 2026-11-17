# Proposal

## Why

Activation reads the typed graph and then discards almost all of it before the agent sees it.
In one personal vault, ten days of served packets (7 to 17 November 2026) carried 7,141 task leads.
Only 19 of those leads listed any link. 10,899 links were read from the graph and then cut,
because the task section charges links last, after every lead and method has filled the allowance.
On a copy of the same vault, 81% of those leads have at least one edge and 54% of servings have a typed one.
The edges exist; the packet drops them.

Anchors fare no better. Their neighbourhood is traversed under the `epistemic` profile, which
keeps only 12% of the edges that touch served leads. At depth 2, 68% of sampled lead pages reach
no neighbour under it, against 17% under all families. The note-to-source path (`derived_from`,
`evidenced_by`, `cites`) is never walked. The neighbours only narrow which units other lanes read.
Their relation is discarded, and the dedicated `graph` lane runs for no shipped role.

When a link does reach the agent, it names a relation and a title, never why the target matters.
An agent cannot judge a bare `links_to` line worth a read, so it does not follow it.

## What Changes

- One connection read serves task leads and resolved anchors. It is a batched read of both
  directions, ranked only by closed relation attributes (`behaviours` and `generic`). Corrections
  and replacements come first, then sources, then other specific relations, then generic and
  associative links. An open planning target ranks first within its tier.
- Every served connection carries a short `why`: the relation and its direction, plus the
  sentence that carries the link in the authoring page, or the target's opening clause when the
  link has no prose. The `why` is released through the same egress rules as unit text.
- A served lead keeps its first connection. Remaining connections fill leftover room in lead
  order, as now.
- Resolved anchors serve their top connections as `pointers[]` through a shipped `connections`
  role on the existing `graph` lane.
- A shipped `recall` traversal profile adds the link, entity, derivation, evidence and citation
  families to the epistemic ones. Activation uses it for anchor neighbourhoods, and a vault can
  extend it.
- The connection line tells the agent to read a connection when its reason bears on the answer.

No public input schema changes. The graph remains optional: when it cannot answer, leads and
anchors are served without connections and the packet reports the gap as today.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: reasoned connections for leads and anchors, the lead-first allocation,
  and the recall profile for anchor neighbourhoods.
- `graph-traversal-profiles`: a shipped `recall` profile.
- `context-roles`: a shipped `connections` role on the `graph` lane.

## Impact

Affected: `working_set` (connection read, graph lane, neighbourhood profile), `context_task`
(allocation and the lead line), `served_context` (records the offered connections and their
tiers), the hook renderer, the core traversal-profile and context-role packs, and the skill
scaffold text. The overlapping active changes `recognize-situations-and-project-methods`
(scenario "Links cost the allowance last") and `promote-referents-without-nudges` (task 8.2,
activation profile and typed neighbours as pointers) are reconciled in this delivery.
