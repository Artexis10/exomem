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
2. It replaces "call `recall_context` once" with a per-subject pull: when the user's turn opens a subject the injected context does not cover, the agent calls `recall_context` with that subject as `conversation.focus`, or `search_memory` with its own query, before it answers. The server instructions carry the same rule for clients without hooks, through `recall_context` and `conversation.focus` only, because their 900-character bound leaves no room for the `search_memory` alternative.

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

A bounded model reading the authored descriptions does recognise it. At one draw on the same 18 cases, Claude Haiku given the product's template and schema outside the product chose exactly the keyed methods in 17 (one false selection on a negative). Through the product itself (`method_check`), a local `qwen3:14b` placement chose exactly in 11 (about 5 s a call) and a local `qwen2.5:7b` in 10 with 8 false selections. No placement served a method in none of the 14 cases that need one. These are development figures: there is no held-out set yet, and the context-interpretation contract asks for held-out evidence before a deployment enables a placement. Selection therefore runs on the context-interpretation boundary that `recognize-situations-and-project-methods` added for optional judgments: a schema-forced answer whose items are the supplied labels, a deadline inside the request budget, no authority, and nothing selected on any failure. It has its own deployment switch, `EXOMEM_METHOD_FIT_CONFIG`, so turning it on does not also turn on lead ordering, whose effect is unmeasured.

The candidates are the visible skill pages and the Records collection manifests, each described by its author: a skill's `description`, a manifest's body. A real vault keeps methods in both. A collection's contract (what one item is, which states need evidence) is a method for writing into it, and the capture owner serves no collection contract per turn.

Rejected:

- The dense chance band on skill bodies or descriptions: measured above.
- A threshold or word list on skill text: a constant belongs to one encoder and vault, and matching words to decide meaning is the pattern registry C6 rejects.
- Leaving selection to the active agent from the catalogue: the existing behaviour, and the dogfood record shows agents act from generic guidance instead.

### Decision: carry the contract

A selected skill carries its released body, cut at a line boundary to the vault's `method_contract_max_chars`. Agent Skills load the body when the task matches; carrying it in the packet does that load for every client, including MCP-only clients with no skill support. A cut body keeps its `read_ref`, so the agent loads the rest in one read. The contract counts against the turn allowance and is cut further before its method is dropped.

### Decision: use evidence at readout, from existing logs

The service already logs reads and writes locally (`reads.jsonl`, `writes.jsonl`) and keeps one served-context row per activation. The readout indexes each log by path once and looks each serving up by time, so it costs one pass over the logs, not one per serving. The owner readout joins them: a later read of the method page is `opened`; a later successful write to a page the method links to, or to an item of a collection it links to, is `followed`. Records item writes were not logged before; they now log under their manifest path. Disabled or rotated logs read `unknown`. Activation does no extra work. The logs are per host and not tied to one conversation, so the readout says so. An agent-declared receipt on every write tool was rejected for this round: it widens seven tool schemas for a self-report the logs can observe.

### Proof

- Packet level, model-free: the no-magic-word fixtures run the real compiler with the real encoder on a seeded vault, and score which methods each turn served against keys written from the operator's intent.
- Agent level, real models: the same cases run through the session-close bench, scoring whether the agent followed the contract and wrote to the keyed destination.
- Installed: an ordinary turn on the installed service serves the matching vault skill with its contract, and the owner readout shows its use evidence.
- Decision model: the method-fit judgment on the same cases with a local placement and an API placement; the per-case exact-match difference against no judgment, with its interval, decides which placement to recommend.

## Catalogue first, selector optional

The agent the owner is talking to is the strongest model in the loop, so Exomem shows it the owner's skills and collections and lets it choose, as Agent Skills clients do with installed skills. This works in every MCP client, with no hooks, no selector model, no provider egress and no added latency.

Evidence, 2026-11-23, invented seed vault, one draw:

- A bounded selector on 24 held-out cases: no selector 8/24 exact, embedding similarity 9/24, GPT-5-nano 17/24, local qwen3:14b 20/24, Claude Haiku 5.5 22/24 (+0.58 [+0.33, +0.79] over none, 2.65 s median).
- Embedding similarity is too weak to cut the catalogue: the right skill is in the top 3 of 11 for 11 of 16 held-out positives.
- End to end with a tool-only Sonnet client on the same cases, before the pointer: the catalogue alone 19/24, the catalogue plus the Haiku selector 21/24; selector minus catalogue +0.08 [−0.08, +0.25], which includes zero.
- On 20 development cases, a `next_action` that points the catalogue's turn at the catalogue lifted the catalogue from 15/20 to 18/20 (+0.15 [0.00, +0.30], 3 won, 0 lost).
- Decision run on 24 fresh held-out cases with the finished catalogue: the catalogue alone 19/24, with the Haiku selector 20/24; selector minus catalogue +0.04 [−0.08, +0.17], which includes zero. A blind judge of what the session then wrote, which sees page edits only, gives 16/24 and 18/24, +0.08 [−0.08, +0.25]. Run records `catalogue-e2e-iter1` and `catalogue-e2e-d2` in the owner's eval-runs collection.
- Correction to the decision run: its scorer counted skills only. Re-scored so that using the keyed Records collection also counts, both arms get 19/24, and the difference is 0.0 [−0.17, +0.17]. The outcome stays the same: the selector stays off.
- Scale run, 2026-11-23, on 24 cases over an invented vault of 13 skills and 90 collections in 12 domains. The listing holds the 13 skills and omits the 90 collections. Each arm used the right item on 14 of 16 positive cases and wrote to no wrong collection: the shipped listing with `turn_items`, a listing scoped to the turn's domains, and the shipped listing with a search pointer. Each alternative minus the shipped listing is 0.0 [−0.13, +0.13], so the shipped shape stays. Agents reached unlisted collections through retrieval (8 of 13), `turn_items` (3) and their own Records listing (5); some were reached more than one way. Both misses were turns that change subject in a client without hooks, where the agent did not refresh context. Run record `catalogue-scale-s1`.

So the catalogue leads, and the selector stays an off-by-default deployment option for the start-of-turn hook path. A catalogue that fits is listed whole, because it is cheap; one that overflows a real vault (about 90 Records collections) keeps one turn-independent listing, so it is still sent once per conversation and pointed at once, and names beside it, on each turn, the unlisted items of the turn's touched domains from the domain registry (then those the task context reached), within their own small bound. No vocabulary enters code.

Text a person or agent reads says skill and collection. The packet keeps its existing field names, such as `task_context.methods`, because renaming them would break every client and hook for no gain to the reader.

## A later subject in a client without hooks

A client without hooks sends Exomem nothing until the agent calls a tool. In the scale run, both misses were conversations that opened with a general question and then turned to the user's own life: the agent made no Exomem call in the whole session. The server therefore has no signal from which to detect the switch, and no word list over the turn may stand in for one. The lever is the text the agent already holds.

The server instructions now ask for recall before answering, per subject, even mid-conversation, and say that the balanced and maximal levels recall on any subject of the user's life or work; before, they defined only the off and light levels. The text stays within its 900-character bound by trimming other wording; every phrase an earlier ruling protects is kept, and the drop of `search_memory` from this text is recorded in the requirement.

Evidence, 2026-11-23, tool-only Sonnet client, one draw, at the balanced level, run record `subject-switch-s1`:

- 12 fresh cases (8 positive, 4 negative) against main: 9/12 exact against 7/12, 2 won and 0 lost, +0.17 [0.00, +0.42]. The interval includes zero, so by itself this is no change. The text ships on the rule written before the run: candidate minus main on exact is at least 0, and no general-only case gains a write. Both wins were sessions in which main made no Exomem call. No fresh negative made a call on either arm.
- The 24 scale cases, against the earlier shipped run (an older base, before the recall tool description was trimmed): exact unchanged at 12/24. Positives fully delivered rose from 14/16 to 16/16, but the gain is the two cases the text was tuned on; on the other 22, exact went up on 2 and down on 3. No write was attempted to a wrong collection on either arm.
- Precision: 7 of 8 scale negatives made an Exomem call, against 6 of 8. The new text also recalled once on a general first turn of a fresh case. The cost is one recall's latency on some general turns, with no writes.
- One fresh case still makes no call after a general first turn on either arm.

## A client that defers tools and hides server instructions

Some chat apps list a third-party server's tools only through tool search and do not show its server instructions. There the only Exomem text the agent holds up front is the skill's description, which triggered on the knowledge base, the vault and notes. The shipped description now names the subjects of the user's own life and work and asks for recall before the agent recommends, asks for a fact or writes. The engagement level that a tool reports still wins: at off, the agent searches only when the user asks.

The session-close bench stands in for such a client with its `deferred-skill` mode: no hooks, only the tool-search and skill built-ins, the skill staged as a project skill, and the server started without its instructions. It is stricter than such an app, which shows the instructions once a tool loads.

Evidence, 2026-11-24, Sonnet client in that mode, one draw, run records `deferred-skill-description-s1` (first text) and `deferred-skill-description-s2` (the text that ships):

- The off level: the first text asked for recall without a condition and searched memory in 5 of 5 off sessions, against 0 of 5 on main. The text that ships says the level a tool reports wins, and searched memory in 0 of 5.
- Regression cases built from two real misses: own-subject cases read memory 1 of 5 times on main and 5 of 5 with the text that ships. General cases made no call on either.
- 12 fresh cases against main: exact 9/12 against 4/12, +0.42 [+0.17, +0.67]; the keyed item was used in 8 of 8 keyed cases against 1 of 8. General cases made no call on either arm. Every attempted write targeted the keyed collection; the bench vault has no collection store and refused most of them.
