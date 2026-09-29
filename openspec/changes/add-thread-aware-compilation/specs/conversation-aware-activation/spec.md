## ADDED Requirements

### Requirement: Bounded optional conversation argument

`activate_context` SHALL accept an optional `conversation` object on every door: MCP, the CLI and the REST facade, all over the same leaf. The object has three optional fields:

- `focus`: a string of at most 240 characters;
- `recent`: a list of at most 6 entries `{role, text}`, where `role` is `user` or `assistant`, ordered oldest first;
- `refs`: a list of at most 12 canonical page refs.

The server SHALL enforce every bound itself, deterministically:

- A `focus` over its bound SHALL be cut at the last whitespace at or before the bound.
- A `recent` entry's text SHALL be cut at the last whitespace at or before 600 characters for a `user` entry and 300 for an `assistant` entry, keeping its head.
- Entries beyond six SHALL be dropped oldest first.
- Entries SHALL then be dropped oldest first until the texts total at most 2,400 characters.
- Refs SHALL be deduplicated keeping first occurrence, non-string refs SHALL be dropped, and refs beyond twelve SHALL be dropped from the end.
- An entry with an unknown role or empty text SHALL be dropped.

A request whose `conversation` is not an object, or whose every field is empty after bounding, SHALL be served exactly as a request without it. The argument SHALL never cause a refusal or an error.

The packet SHALL report `generation.conversation`:

- `absent` when nothing survived bounding;
- `truncated` when any cut or drop above was applied;
- `applied` otherwise.

The value SHALL be computed before any visibility filtering. A request without `conversation` SHALL produce a packet byte-identical to the packet it produces before this change, apart from `generation.conversation = "absent"`. The argument is distinct from the continuity token's keyless thread (`generation.continuity_thread`) and SHALL NOT read or change it.

The CLI SHALL expose the fields as:

- `--focus`;
- repeatable `--recent-user` and `--recent-assistant`, kept in the order given;
- repeatable `--conversation-ref`.

#### Scenario: Omitting the conversation changes nothing

- **WHEN** a turn is compiled with no `conversation`, before and after this change, against the same vault state, index generation and continuity token
- **THEN** anchors, roles, units, pointers, current state, missing, ambiguity and budget are identical
- **AND** the new packet reports `generation.conversation = "absent"`

#### Scenario: An oversized conversation is bounded, not refused

- **WHEN** a caller sends ten `recent` entries of 2,000 characters each, a 1,000-character `focus` and thirty refs
- **THEN** the packet is served, compiled from at most six entries totalling at most 2,400 characters with the newest kept, a `focus` of at most 240 characters and the first twelve distinct refs
- **AND** it reports `generation.conversation = "truncated"`

#### Scenario: A malformed conversation is ignored

- **WHEN** `conversation` is a string, or every entry has an unknown role
- **THEN** the packet equals the packet for the same request without `conversation`
- **AND** it reports `generation.conversation = "absent"`

### Requirement: Focus is resolved as part of the current turn

A `focus` string SHALL be resolved as a second segment of the current turn, contributing the worded contact kinds `exact_alias`, `lexical_overlap` and `claims_match`, and the qualifiers those kinds admit. It SHALL NOT run a recall query or an embedding, so it contributes neither `retrieval` nor `vector_band`. An anchor reached through `focus` SHALL record which segment reached it (`turn`, `focus` or both), beside its evidence kinds.

Tokens SHALL NOT pair across the two segments:

- An n-gram, a proximity window or a two-stem rarity pair SHALL be formed within one segment only.
- The retrieval carry, the referential test and the follow-up test SHALL read the turn segment alone. A turn that speaks only a referential cue therefore stays referential when a `focus` accompanies it.

#### Scenario: The agent's focus supplies the second domain

- **WHEN** a turn weighs "the budget trade-off we discussed", and the agent's `focus` names "Harbor Lantern budget versus the Kestrel hiring plan"
- **THEN** both anchors named by exact alias in `focus` resolve
- **AND** each records the segment `focus`

#### Scenario: Focus does not make a referential turn non-referential

- **WHEN** the turn is "continue" and a `focus` names one anchor by alias
- **THEN** the turn is still referential for the recency rules
- **AND** that anchor resolves through `focus` on its own evidence

### Requirement: Conversation evidence is a subordinate qualifier

Earlier turns (`recent`) and read refs (`refs`) SHALL contribute exactly one evidence kind, `conversation`. It is a qualifier, and it SHALL:

- never create a candidate;
- never resolve an anchor on its own, or together with qualifiers only;
- never count toward the two-kinds rule, except as provided below.

An anchor SHALL carry `conversation` when either of these holds:

- its canonical ref is among the visible refs;
- it is reached by `exact_alias` or `lexical_overlap` within one `recent` entry, under the turn's own token and stopword rules, evaluated per entry and never across entries.

Only the newest three `user` entries and the newest two `assistant` entries SHALL be read for this purpose. Conversation text SHALL NOT be embedded, sent to recall, or read by any model.

An anchor that carries `conversation` together with at least one contact kind reached by the current turn, in either segment, SHALL resolve. This is the same deliberate exception `continuity` has: the conversation already named that subject, and the current turn reached it again. `conversation` and `continuity` on one anchor SHALL count once.

#### Scenario: The conversation promotes a partial second domain

- **WHEN** a turn reaches one anchor by `exact_alias` and a second only by `lexical_overlap`, and a `recent` user entry names the second by its alias
- **THEN** both anchors resolve, and the second's evidence is `[lexical_overlap, conversation]`
- **AND** the same turn without `conversation` resolves only the first and lists the second as `partial`

#### Scenario: The conversation alone resolves nothing

- **WHEN** a turn that is neither referential nor anaphoric reaches no anchor by any contact kind, and the earlier entries name three anchors by alias
- **THEN** the packet abstains `unresolved`, exactly as it does without `conversation`

#### Scenario: Words split across entries do not form a name

- **WHEN** one `recent` entry ends with "Harbor" and the next begins with "Lantern"
- **THEN** no anchor named "Harbor Lantern" carries `conversation` from that pair

### Requirement: The conversation breaks a tie only when it is unambiguous

When the current turn is `ambiguous` and exactly one of the competing resolved anchors carries `conversation`:

- that anchor SHALL be served as resolved;
- the others SHALL be listed as `partial`;
- the packet SHALL report `generation.disambiguated_by = "conversation"`.

When none or two or more competitors carry `conversation`, the turn SHALL stay `ambiguous` exactly as without it.

#### Scenario: The conversation settles which hub was meant

- **WHEN** a turn resolves two same-kind hubs that share no anchor neighbour, and only one of them was named in the newest user entry
- **THEN** that hub is served resolved with `generation.disambiguated_by = "conversation"`
- **AND** the other is listed `partial`

#### Scenario: A conversation naming both keeps the ambiguity

- **WHEN** both competing hubs carry `conversation`
- **THEN** the packet is `ambiguous` and lists both

### Requirement: An anaphoric turn is carried from the newest conversation subject

A turn SHALL be anaphoric when both of these hold:

1. Its turn segment reaches no anchor by any worded contact kind.
2. It speaks a cue from the effective referential vocabulary, or one of a closed, shipped anaphor set: third-person pronouns and possessives; "that", "this", "those", "these", "it"; the follow-up markers already shipped; an ordinal followed by "one" or "option"; "the former" and "the latter".

Length is not a criterion: an anaphoric turn MAY carry any number of other words.

The carry SHALL run for an anaphoric turn only when all three hold:

- it has no `focus` contact;
- no recency, continuity or follow-up rule has already resolved or carried an anchor for it;
- the conversation has at least one visible `user` entry.

The compiler SHALL then walk the visible `user` entries of `recent` from newest to oldest. It SHALL stop at the first entry in which at least one anchor resolves under the ordinary rules applied to that entry's text alone, with no recall query and no embedding.

- **Exactly one anchor.** That anchor SHALL be carried as the packet's single anchor:
  - at status `partial`, with evidence `[conversation]`;
  - its material served under the ordinary lanes;
  - `generation.carried_by = "conversation"`.
- **Two or more.** The turn SHALL abstain `ambiguous` listing them, and SHALL NOT choose.
- **No entry resolves an anchor.** The turn SHALL abstain exactly as without `conversation`.

A refs-only conversation SHALL NOT carry: a ref says what was read, not what "it" refers to. A carried anchor, being `partial`, SHALL NOT enter the continuity token.

#### Scenario: A rich follow-up keeps the conversation's subject

- **WHEN** an earlier user entry resolved the invented entity "Ottilie Marsh", and the current turn is "Given everything above, how did her results compare with the spring round, and should we change anything before the next one?"
- **THEN** "Ottilie Marsh" is carried as a single `partial` anchor with evidence `[conversation]` and `generation.carried_by = "conversation"`
- **AND** the same turn without `conversation` abstains `unresolved`

#### Scenario: The newest subject wins over an older one

- **WHEN** the newest user entry resolved "Kestrel hiring plan", an older one resolved "Harbor Lantern budget", and the turn is "what are the risks with that?"
- **THEN** "Kestrel hiring plan" is carried
- **AND** nothing from "Harbor Lantern budget" is served

#### Scenario: A turn that names its own subject is not carried

- **WHEN** a turn uses a pronoun but reaches an anchor by its own words
- **THEN** no carry runs, and the conversation contributes only the `conversation` qualifier

### Requirement: The current turn is never drowned by the conversation

When the current turn resolved at least one anchor by its own segments:

- Conversation-promoted material SHALL use at most one third of `max_chars`.
- Anchors the turn resolved without `conversation` SHALL be served first, in role order, and conversation-promoted anchors after them.
- Material beyond that share SHALL become pointers, or `missing[] {role, reason: "budget"}` as usual.

The conversation SHALL NOT add an anchor the current turn did not reach, except through the carry rule for an anaphoric turn. Because only the newest entries are read, a longer conversation never raises what an older subject can contribute.

#### Scenario: A long conversation about one subject, a turn about another

- **WHEN** six `recent` entries all discuss "Harbor Lantern budget", and the current turn names "Kestrel hiring plan" by alias and speaks no anaphor
- **THEN** only "Kestrel hiring plan" resolves
- **AND** nothing from "Harbor Lantern budget" is served

#### Scenario: Promoted material keeps within its share

- **WHEN** a turn resolves one anchor on its own and a second through `conversation` promotion, and both neighbourhoods exceed `max_chars`
- **THEN** the second anchor's units use at most a third of `max_chars`
- **AND** the first anchor's units are emitted first

### Requirement: Conversation content is ephemeral and never crosses callers

Conversation content SHALL be request-scoped.

**What is never written.** No conversation text, and no digest or hash of it computed by the activation path, SHALL be written to any of:

- the vault or the activation index;
- the packet cache or the heat projection;
- the activation log (`activations.jsonl`);
- the episode ledger or capture state;
- timings, logs or any other machine-local state file.

**Call ledger.** The call ledger SHALL keep its existing shape-and-hash record of arguments (call-ledger: "Arguments Are Recorded As Shape And Hash, Never Values"), which covers `conversation` exactly as it covers `turn`.

**Activation log.** The activation log MAY record only these, per call:

- whether `conversation` was present;
- the count of surviving `recent` entries and refs;
- the `generation.conversation` value.

**Hot profile.** Conversation refs SHALL NOT be recorded as a pick, read, citation or any other hot-profile event.

**Packet cache.** A request carrying a non-empty `conversation` SHALL be neither served from nor stored in the packet cache.

**Packet and token.**

- No packet field SHALL quote conversation text. Every served string still comes from vault material under the egress guard.
- The continuity token SHALL NOT encode any conversation field, a carried anchor, or a promoted anchor that did not also resolve without `conversation`.

**Hooks.** Hooks SHALL NOT forward conversation text to capture or episode recording.

#### Scenario: Machine-local state holds no conversation text

- **WHEN** a request carries a `recent` entry containing a distinctive invented phrase
- **THEN** after the call, no file under the vault's machine-local state root contains that phrase
- **AND** no file other than the call ledger contains its sha256

#### Scenario: A request with a conversation bypasses the cache

- **WHEN** two requests carry the same turn but different conversations
- **THEN** the second is compiled afresh, and is not served the first's packet

### Requirement: Withheld conversation refs are absent

For a caller other than the owner under a governed policy:

- A conversation ref naming a page withheld from that caller SHALL be dropped before evidence is assembled, exactly as an unknown ref is.
- Conversation text SHALL be matched against the caller's view of the catalogue (context-activation: "Activation resolves a turn over the caller's view"), so a withheld anchor is never reached from earlier turns either.
- The packet, `generation` included, SHALL be identical to the packet for the same request whose conversation never named that page.

#### Scenario: A withheld ref answers as an absent one

- **WHEN** a restricted caller's `refs` name a withheld page and a visible page
- **THEN** its packet is byte-identical to the packet for `refs` naming only the visible page
- **AND** `generation.conversation` has the same value in both

### Requirement: Remote agents are told how to pass the conversation

The server instructions SHALL keep asking the agent to pass the user's message verbatim as `turn`. They SHALL add one sentence, pinned below. The tool description and the shipped skill scaffold SHALL state:

- the field bounds;
- that every field is optional;
- that `turn` stays verbatim.

None of them SHALL ask the agent to summarise the conversation into `turn`, or to paste whole earlier turns. The pinned sentence is:

"In a longer conversation also pass `conversation`: `focus`, one line naming the subjects now in play, and `refs`, the pages you already read; never rewrite `turn`."

The server instructions SHALL stay within the length budget the existing server-instructions test enforces.

#### Scenario: Instructions carry the conversation sentence

- **WHEN** a client reads the server instructions at `initialize`
- **THEN** they contain the pinned sentence
- **AND** they still ask for the message verbatim

### Requirement: Pre-registered conversation benchmark group

The context-activation audit SHALL add a group named `conversation`, authored and digest-pinned in a commit that precedes its first scored run. It SHALL use invented names only.

The group SHALL hold at least:

- twelve rich single turns of two to five sentences, at least four of which span two domains;
- twelve multi-turn conversations of three to six earlier entries plus a current turn;
- one negative twin per case: the same current turn with a conversation about unrelated invented material, or the same conversation with a current turn that reaches nothing;
- three drowning cases, where a long conversation about one subject is followed by a current turn about another;
- three topic-switch cases, where the newest subject is gold and the older one is poison;
- one withheld-versus-absent pair, scored for byte identity.

Every case SHALL pre-register:

- gold and poison anchors;
- must-include and must-exclude facts;
- the expected status and the expected `generation.carried_by`.

The group SHALL be scored on four arms:

- the turn alone, which is the mechanism-removal control;
- the turn with `recent` and `refs`, as a hook sends them;
- the turn with a fixture-authored `focus`, as a remote agent sends it;
- the turn with all three.

The pre-registered floors, per case and per anchor kind, SHALL be:

- gold recall of at least 0.85 on every arm that passes a conversation;
- zero poison;
- zero `resolved` false activation on twins;
- zero drowning failures;
- the arm without conversations failing at least half of the multi-turn cases;
- the audit's existing packet-size bounds, unchanged.

Editing a fixture after a run manifest that references its digest exists SHALL void that run, never rescore it.

#### Scenario: The conversation arm must beat its own removal

- **WHEN** the group is scored with `conversation` stripped from every request
- **THEN** at least half of the multi-turn cases fail
- **AND** the audit reports the mechanism-removal arm red

#### Scenario: A drowning case fails loudly

- **WHEN** any drowning case serves material from its conversation's earlier subject
- **THEN** the case fails regardless of its recall

### Requirement: Conversation stage latency budget

Conversation handling SHALL be timed as its own registered stage, `working_set.conversation`. The stage covers:

- bounding;
- visibility filtering of refs;
- per-entry matching;
- carry selection.

On the model-free synthetic reference corpus used by the CI latency gate, with a maximum-size conversation, the gate SHALL pin:

- the `working_set.conversation` stage at p95 of at most 60 ms;
- warm `activate_context` at p95 of at most 1,000 ms.

A conversation stage that cannot start within the request's remaining reserve SHALL be skipped, and the packet SHALL then be compiled exactly as without `conversation` and report `generation.conversation = "absent"`. The skip SHALL NOT fail the request.

#### Scenario: A spent deadline drops the conversation, not the packet

- **WHEN** the request deadline leaves less than the conversation stage's reserve
- **THEN** the packet is compiled without the conversation
- **AND** it reports `generation.conversation = "absent"`
