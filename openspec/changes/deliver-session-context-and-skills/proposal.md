# Proposal

## Why

A vault can hold Agent Skills: directories with a `SKILL.md` page whose frontmatter names the skill and describes when to use it.
Activation never presents them as skills.
Their units index as ordinary prose, so method projection never selects them, and the catalogue stores the body lede instead of the trigger description.
An agent only reaches a vault skill when a person points at its path.

Native clients load skills by progressive disclosure: every skill's name and description sit in context, and the agent loads a body only when the task matches.
Clients without native skills, such as chat apps connected over MCP, get none of this from the vault.

Ordinary turns also miss context for a separate reason: the start-of-turn hook delegates activation to the agent with an instruction to call recall once, and agents seldom call it again.
That delivery problem is recorded here and settled by later tasks in this change once measured.

## What Changes

- Recognize a skill page by the published Agent Skills format: a page named `SKILL.md` whose frontmatter carries string `name` and `description`. The product holds no list of skills.
- Store a skill page's frontmatter `description` in the context catalogue as its description, so selection and presentation use the author's trigger text.
- Serve a turn-independent `skills` block in maps responses: each visible skill's name, the first sentence of its description and its load reference. Serve it once per conversation and again only when it changes, using the existing continuity token.
- Project a skill page that task discovery finds relevant as a method item that carries its full description and load reference.
- Let a caller name the skills it already loads natively, so the catalogue omits them.

## Capabilities

### New Capabilities

- `vault-skills`: recognition, catalogue presentation, turn-relevant projection and loading of Agent Skills authored in the vault.

### Modified Capabilities

- None in canonical specs yet. Later tasks add the measured delivery change to `retrieve-inject-hook`.

## Impact

The implementation reuses the context catalogue, the maps packet, continuity generations, task discovery lanes, method projection and existing egress.
It adds no retrieval engine, no skill registry and no second store.
Skills stay ordinary governed pages: admission, visibility and egress decide what each caller receives.
This change builds on `recognize-situations-and-project-methods` (methods) and `add-stance-block-and-served-context` (turn-independent blocks); their open tasks stay accounted for there.
