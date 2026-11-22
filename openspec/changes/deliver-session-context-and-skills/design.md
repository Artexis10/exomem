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

The continuity token tells the service which map generation the session holds, and the service then answers `unchanged`. So in both modes the hook reserves room for the map's floor before the stance: the map's ref, the count of items the hook left out and the overflow routes. An upkeep item claims its room before the floor, because the service records the item delivered when it attaches it and a dropped item is lost. With that floor the agent can pull the whole map by its ref, so the hook persists the token once the map renders whole or as its floor. A changed map that renders in no form keeps the previous token, and the next prompt's one request receives the map again. A stance that loses its room to the floor arrives on the next prompt, because the service sends the stance on every packet and the hook records only a stance it showed whole.

The service attaches no upkeep item and no vocabulary delta to an empty-turn request. It records each one delivered when it attaches it, and the hook may abandon the session fetch at its deadline, which would lose it. The agent's first recall with a turn receives them instead. The other blocks a packet carries record no delivery on the service, or record it through the continuity token that the hook persists only after the agent received the injection.

Working-set mode keeps its turn packet. Turn leads stay pull-based until a lane grounds raw turns well enough to pay for its hook time; the presentation fixes in the next round are measured against the same replay.

### Rejected

- Push the full turn packet on every prompt (working-set mode everywhere): about 1.5 s median per prompt for a 3-in-23 delivery rate.
- Keep "once": agents stop recalling after the first turn, so later subjects never reach memory.
- Push `search_memory` hits for the raw turn: cheaper, but conversational turns rank recorded transcripts above notes.

## Method activation from ordinary turns

### What the installed service showed

On 2026-11-22 the installed service answered an ordinary technology turn in 22.4 s. The method-unit lane took 10.9 s and found nothing, and the conversation carry took 5.9 s. On a first turn of the same day the budget ran out before the method lane started. Skill pages reach `task_context` only when the general text or dense lanes happen to rank them among a 1,500-page vault, so a real vault rarely projects a skill. Task 4.1 passed on a three-page fixture, where any lane finds the skill. The served method item carried a description and a pointer, and `served_context_evidence` is a constant `unknown`.

### Decision: a bounded judgment selects methods from their authored descriptions

The first design kept skill pages whose chunks cleared the dense chance band. It was measured before it shipped, on the no-magic-word fixtures (18 cases on a 73-page seeded vault with 10 skills, keys written by a lane that never saw the implementation). Topical similarity does not recognise a situation: for the 14 cases that need a method, the keyed skill was the closest skill by its body in 4, and the closest by its description in 4. A turn is about a database or a dentist; a skill describes what to do when the user approves a trial or reports a failure.

A bounded model reading the authored descriptions does recognise it. At one draw on the same 18 cases, Claude Haiku chose exactly the keyed methods in 17 (one false selection on a negative), and a local `qwen2.5:7b` in 7. Selection therefore runs on the context-interpretation boundary that `recognize-situations-and-project-methods` added for optional judgments: a schema-forced answer whose items are the supplied labels, a deadline inside the request budget, no authority, and nothing selected on any failure. It has its own deployment switch, `EXOMEM_METHOD_FIT_CONFIG`, so turning it on does not also turn on lead ordering, whose effect is unmeasured.

The candidates are the visible skill pages and the Records collection manifests, each described by its author: a skill's `description`, a manifest's body. A real vault keeps methods in both. A collection's contract (what one item is, which states need evidence) is a method for writing into it, and the capture owner serves no collection contract per turn.

Rejected:

- The dense chance band on skill bodies or descriptions: measured above.
- A threshold or word list on skill text: a constant belongs to one encoder and vault, and matching words to decide meaning is the pattern registry C6 rejects.
- Leaving selection to the active agent from the catalogue: the existing behaviour, and the dogfood record shows agents act from generic guidance instead.

### Decision: carry the contract

A selected skill carries its released body, cut at a line boundary to the vault's `method_contract_max_chars`. Agent Skills load the body when the task matches; carrying it in the packet does that load for every client, including MCP-only clients with no skill support. A cut body keeps its `read_ref`, so the agent loads the rest in one read. The contract counts against the turn allowance and is cut further before its method is dropped.

### Decision: use evidence at readout, from existing logs

The service already logs reads and writes locally (`reads.jsonl`, `writes.jsonl`) and keeps one served-context row per activation. The owner readout joins them: a later read of the method page is `opened`; a later successful write to a page the method links to, or to an item of a collection it links to, is `followed`. Records item writes were not logged before; they now log under their manifest path. Disabled or rotated logs read `unknown`. Activation does no extra work. The logs are per host and not tied to one conversation, so the readout says so. An agent-declared receipt on every write tool was rejected for this round: it widens seven tool schemas for a self-report the logs can observe.

### Proof

- Packet level, model-free: the no-magic-word fixtures run the real compiler with the real encoder on a seeded vault, and score which methods each turn served against keys written from the operator's intent.
- Agent level, real models: the same cases run through the session-close bench, scoring whether the agent followed the contract and wrote to the keyed destination.
- Installed: an ordinary turn on the installed service serves the matching vault skill with its contract, and the owner readout shows its use evidence.
- Decision model: the method-fit judgment on the same cases with a local placement and an API placement; the per-case exact-match difference against no judgment, with its interval, decides which placement to recommend.
