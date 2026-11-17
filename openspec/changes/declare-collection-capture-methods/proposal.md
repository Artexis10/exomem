# Proposal

## Why

An owner can declare where a kind of capture belongs, and write down how to capture it, yet neither reaches the agent that is capturing.
On one incident a note's project was lost on write, so a collection that declared the note's type and project lost the route to a collection that merely shared more words with it.
The owner's procedure for capturing into that collection was ordinary prose that nothing could point at.
Today a collection declaration orders candidates only when it matches in full, a strong route carries an action only for failure-shaped notes, and no manifest field names the page that holds the method.

## What Changes

- Rank collections that hold part of their declared `claims.match` ahead of undeclared word routes, by the number of predicates held, then by coverage. A partly held declaration orders candidates and never makes a route `strong`; the coverage floor, the subject-signal gate and silence on a tie stay.
- Give every strong route that a full declaration produced its disposition (`file`, `ask`, `hold`, `append_occurrence`), whatever the note's type. The failure-shaped gate remains for strong routes no declaration produced.
- Add an optional manifest block `capture: {method: <page>}` naming one vault page of any kind. Resolve it for the caller asking; an unresolved or withheld page is one visible inspection finding and an omitted method, never a routing failure.
- Name the resolved method on the write advisory as `method {title, read_ref}`, and have an acting or asking instruction tell the agent to follow it.
- Serve the declared page at turn time when a routed collection leads the task context, through the existing method kinds and source-version reset.
- Document the field in the shipped skill scaffold and regenerate its derived copies.

## Capabilities

### Modified Capabilities

- `command-surface`: partial-declaration ranking, declared-route dispositions and the `method` key of the Records routing advisory.
- `attention-queue`: the unreflected-observation family restates the routing rule it inherits.
- `structured-collections`: the `capture.method` manifest field, its resolution and its finding.
- `context-activation`: a routed collection lead serves its declared method.

## Impact

Builds beside `recognize-situations-and-project-methods`: this change adds a pointer and its delivery, and no workflow store, scheduler or second method catalogue.
The page stays user-authored governed knowledge; the manifest holds only the reference.
Everything reuses the router, the link resolver, the method projection and the governance release decision.
Egress is in scope: a declared page that the caller may not read is omitted whole, indistinguishably from a missing one, on the write advisory, in inspection and in the turn packet.
The change adds no new retrieval lane. A route that declares a method costs one reference lookup, plus one source projection at turn time; a collection that declares nothing costs nothing.
The turn-time method has one boundary: removing the declaration while the method page stays unchanged does not reset what a client already holds, because the held method's own source is unchanged; the reset follows the page's next revision.
`MIN_CLAIM_COVERAGE` and `FAILURE_SHAPES` are provisional constants that this change leaves as they are; the second is old debt kept only for undeclared routes.
