## Decision

The Stop hook is a trigger, not a carrier of doctrine. The capture rules that used to
ride in the first-fire text (decompose before routing, hypotheses stay attributed, the
entity-recurrence review cadence and its rechecks, create-entity conditions) are in
`_Schema/references/engagement.md` and `operations.md`, deployed to `.exomem/schema/` in
every vault initialised or refreshed since #488 (see Pointer). The served `bootstrap` core carries the routing and artifact-adoption parts
and is at its byte ceiling, so the hook points at the reference instead of growing the
core.

## Pointer

`read_memory .exomem/schema/references/engagement.md`. `read_memory` opens that path in a
vault initialised or refreshed since #488 (checked). A pre-#488 legacy vault keeps its shipped
schema under `Knowledge Base/_Schema/` and nothing migrates it automatically, so the pointer is
NOT_FOUND there; that is the known gap, and the hook carries no fallback path. The pointer is
defined by `SHIPPED_SCHEMA_DIRNAME` plus the shipped-schema globs; a test ties the pointer to both so a rename fails there.

## Trade-off

An MCP-connected agent that never opens the pointer does not see the entity-recurrence
review cadence on a landing. The short check still routes intent, outcomes and
supersession, which are the incident rules, and the skill-capable clients carry the
reference in the installed skill.
