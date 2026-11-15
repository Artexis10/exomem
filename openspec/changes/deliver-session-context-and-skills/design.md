# Design

## Context

Native clients keep every installed skill's name and description in context and load a skill body when a task matches.
Exomem can reach every client, including chat apps that have no native skills, but it presents a vault skill only as an unlabelled retrieval lead.

The catalogue already indexes skill pages as notes. It stores the body lede as the description, and no flag marks the page as a skill.
Method projection selects semantic units of method categories, and skill bodies index as prose, so it never selects a skill.

## Decisions

### Recognize skills by the published format

A page is a skill when its file name is `SKILL.md` and its frontmatter carries string `name` and `description`.
The Agent Skills format fixes this closed shape, so checking it encodes no vocabulary.
Location does not matter, and the product lists no skills.
Sources and Evidence are excluded because captured material is evidence, not instruction.

### Store the trigger text in the catalogue

The catalogue gains a `skill` flag, indexed like `stance`.
A skill row stores its frontmatter description instead of the body lede.
The catalogue version increments, so existing rows rebuild through the normal catalogue update.

### Serve the catalogue once per conversation, outside the turn budget

The catalogue is session context, like a native skill listing in a system prompt.
Measured on a 19-skill vault, full descriptions take about 8,000 characters, first sentences about 3,300 and names alone about 300.
Names alone do not let an agent choose; full descriptions every turn would crowd out turn context.
The block therefore carries first sentences under its own bound and is sent only when its generation differs from the one in the caller's continuity token.
The bound and the summary length are the vault's `budgets` in its activation conventions (shipped defaults 3500 and 200), because a vault's skill count and phrasing set the right size, not the product.
Later turns receive `unchanged` and spend nothing.
The `max_chars` allowance keeps its meaning: it bounds turn context.

### Project relevant skills as methods with their full trigger text

When task discovery admits a skill page, the response projects it as a method item of kind `skill` with the full description.
This reuses method projection, continuity method versions and the existing read-before-applying rule.

### Let clients omit native copies

A client that already loads some skills natively names them in `native_skills`.
The local hook can list its client's native skill directories without a model; an MCP-only client sends nothing and receives the whole catalogue.
Omission applies only to the catalogue, so a native copy can still surface as a relevant method.

### Reach a skill's other files by the paths it names

A skill's body names its references and scripts by paths relative to its folder, and `read_page` returns the skill's own path, so each named file is one read away.
Opening a skill lists no directory: a listing must cost a bounded read and must not let pages the caller cannot see change what it shows, and visibility is a per-page decision the catalogue cannot index.
A listing returns only with an indexed folder read that applies visibility before its bounds.

## Risks

- A large vault of skills could exceed the catalogue bound. Whole items fit in name order and `omitted` reports the rest; the agent can still find omitted skills through task discovery.
- A client that ignores continuity receives the catalogue on every turn. The bound caps that cost.
- Adding a block to every maps response adds work: one indexed catalogue query for the skill rows and their admission checks, even when the caller already holds the block. The change proves recall latency before and after on the recall latency instrument; a per-caller cache waits for evidence that the query costs enough to need one.

## Delivery on every turn

### What the baseline showed

A model-free replay sent 23 real operator turns from one week to the installed service and checked whether the page that answered each turn reached the response.
`recall_context` on the raw turn delivered it in 3 of 23 cases; `search_memory` on the same text found it in the top ten in 7.
Nine of the turns were a path to a handoff file plus fixed wording, which no lane can ground: the needed context is in the file the agent reads next.
Every response reported its task candidates cut by budget, and the turn slice carried generic or wrong anchors on personal turns.
An earlier replay showed queries written by the agent after it understood the task reached the needed page far more often than either lane on the raw turn.
So pushing turn leads on every prompt would add a second of hook time for little gain, while the turn-independent context is cheap and always right.

### Decision

In native activation mode the start-of-turn hook does two things:

1. It fetches session context: a `recall_context` call with an empty turn and the held continuity token. An empty turn runs no turn stage, so the response carries only the map, stance and skills blocks, each `unchanged` when the caller already holds its generation. Measured on the installed service, this takes about 50 ms of server time warm and 360 ms cold. The hook injects only the blocks that changed, under a session-context data header that does not claim the turn was activated, and then the activation text.
2. It replaces "call `recall_context` once" with a per-subject pull: when the user's turn opens a subject the injected context does not cover, the agent calls `recall_context` with that subject as `conversation.focus`, or `search_memory` with its own query, before it answers. The server instructions carry the same rule for clients without hooks.

The fetch has its own wall budget, separate from the turn packet's. The budget covers credential discovery as well as the request, because reading the service's environment file can block. The fetch sends one request to the first credentialed lane and never retries. A timeout, a transport failure, an HTTP error or a host with no credentialed REST lane injects the activation text alone, as today, and logs the outcome. Clients without a lane still receive session context from their first `recall_context` call.

The continuity token tells the service which map generation the session holds, and the service then answers `unchanged`. So in both modes the hook withholds the token the first time the render ceiling cuts a map generation, and the next request receives the map again at the smaller cap that the cut taught the hook. A second cut of the same generation is acknowledged, so a map that cannot fit renders twice, not on every prompt.

Working-set mode keeps its turn packet. Turn leads stay pull-based until a lane grounds raw turns well enough to pay for its hook time; the presentation fixes in the next round are measured against the same replay.

### Rejected

- Push the full turn packet on every prompt (working-set mode everywhere): about 1.5 s median per prompt for a 3-in-23 delivery rate.
- Keep "once": agents stop recalling after the first turn, so later subjects never reach memory.
- Push `search_memory` hits for the raw turn: cheaper, but conversational turns rank recorded transcripts above notes.
