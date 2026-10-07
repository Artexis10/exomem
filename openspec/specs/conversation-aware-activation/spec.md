# conversation-aware-activation Specification

## Purpose
Let `activate_context` read a bounded excerpt of the conversation, so a follow-up turn
keeps the subject its thread established. The agent's `focus` resolves as part of the
current turn; earlier turns and refs only qualify candidates, break ties and carry an
anaphoric turn's subject. The conversation never outweighs the current turn, never
persists, and never crosses to another caller.

## Requirements

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

A `focus` string SHALL be resolved as a second segment of the current turn, contributing the worded contact kinds `exact_alias`, `lexical_overlap` and `claims_match`, and the qualifiers those kinds admit. It SHALL NOT run a recall query or an embedding, so it contributes neither `retrieval` nor `vector_band`.

Every served anchor SHALL carry an `origin` naming whose words reached it, so the agent can tell what the user said from what the agent itself said was in play:

- `turn`: reached by the turn segment only;
- `focus`: reached by the `focus` segment only;
- `turn_and_focus`: reached by both;
- `conversation`: carried from an earlier user entry (see the carry requirement below).

An anchor whose only contact came from `focus` SHALL keep `origin = "focus"` whatever qualifiers it gathers. A `focus` origin SHALL NOT count as the agent's `anchor` choice (`agent_choice`), and SHALL NOT override an ambiguity between anchors the turn segment itself resolved. The working-set hook's rendered block and the tool description SHALL state that `focus`-origin material is there because the agent named it, not because the user did. A packet built without `focus` SHALL label every anchor `turn`, or `conversation` when carried.

Tokens SHALL NOT pair across the two segments:

- An n-gram, a proximity window or a two-stem rarity pair SHALL be formed within one segment only.
- The retrieval carry, the referential test and the follow-up test SHALL read the turn segment alone. A turn that speaks only a referential cue therefore stays referential when a `focus` accompanies it.

#### Scenario: The agent's focus supplies the second domain

- **WHEN** a turn weighs "the budget trade-off we discussed", and the agent's `focus` names "Harbor Lantern budget versus the Kestrel hiring plan"
- **THEN** both anchors named by exact alias in `focus` resolve
- **AND** each carries `origin = "focus"`

#### Scenario: Focus cannot settle the user's own ambiguity

- **WHEN** the turn segment resolves two competing hubs, and `focus` names one of them
- **THEN** the packet stays `ambiguous`, and that hub carries `origin = "turn_and_focus"` in the ambiguity list

#### Scenario: Focus does not make a referential turn non-referential

- **WHEN** the turn is "continue" and a `focus` names one anchor by alias
- **THEN** the turn is still referential for the recency rules
- **AND** that anchor resolves through `focus` on its own evidence

### Requirement: Attachment-derived cues travel in focus and never as authority

A client whose own vision or file layer read names or objects from the user's attachments (images, screenshots, documents) MAY put those cues into `focus`, within its bound. The server SHALL NOT distinguish them from any other `focus` text, so they SHALL:

- resolve with the worded kinds of the `focus` segment only;
- be labelled `origin = "focus"`;
- never count as the user's words, as an `anchor` choice, or as evidence of anything beyond contact;
- be request-scoped under the same retention rules as all conversation content.

Activation SHALL NOT receive, fetch or decode an attachment. It SHALL NOT run OCR, CLIP, captioning or any other media model, and it SHALL add no media stage. The tool description and the shipped skill scaffold SHALL tell the agent it may do this.

#### Scenario: Screenshots with near-empty text reach their subject through focus

- **WHEN** the user sends two screenshots with the text "thoughts on this?", and the client's vision layer read the invented names "Ottilie Marsh" and "Tidewater grant" from them and put both into `focus`
- **THEN** both anchors resolve through `focus` with `origin = "focus"`
- **AND** the same turn without `focus` abstains `unresolved`, exactly as it does today
- **AND** no media model or attachment read runs inside activation

#### Scenario: A misread cue reaches nothing

- **WHEN** `focus` carries attachment-derived words that name no anchor by any worded kind
- **THEN** the packet is the packet for the same turn without `focus`, apart from `generation.conversation`

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

A turn SHALL be carried from the conversation only when all four of these hold:

1. **It resolves nothing of its own.** Its turn segment reaches no anchor by any worded contact kind.
2. **It points back.** It speaks a cue from the effective referential vocabulary, or one of a closed, shipped set: third-person pronouns and possessives; "that", "this", "those", "these", "it"; the follow-up markers already shipped, except the bare pointers "one", "ones" and "other"; an ordinal followed by "one" or "option"; "the former" and "the latter"; an elliptical "what about …" or "how about …" opener. A bare pointer counts only when a determiner ("the", "a", "an"), a demonstrative, an ordinal or "which" governs it ("that one", "the other one", "which one"); a numeral use ("for one day", "no one") does not. Contractions are split first ("it's" is "it is"). A demonstrative that places a time ("this week", "these days") does not point back.
3. **Its content is licensed by the subject.** Every content word SHALL belong to the selected subject's own name/title or the frozen generic task vocabulary, compared through the same `forms()` normalisation. A word merely appearing in an earlier user or assistant turn SHALL NOT license a carry. Function words, light verbs, pointers, numbers and the vault's referential vocabulary retain their existing neutral treatment. Time words SHALL neither license nor block the content gate. An unlicensed content word SHALL prevent the conversation carry.
4. **It supplies no turn-local material.** Before neutral-word removal or title licensing, the compiler SHALL veto the conversation carry when the current turn structurally introduces a nominal/value, quotes an explicitly delimited expression, or supplies an alternative set that a selecting pointer addresses. Task, time, numeric and neutral words SHALL remain eligible heads for this check. A pointer-headed historical expression or temporal adjunct SHALL NOT alone establish an introduction; coordination alone SHALL NOT establish a local choice. A detected local-material class SHALL veto the entire conversation-carry opportunity, including mixed local/historical turns, without attempting general coreference.

The local-material check SHALL retain surface clause/list and quotation boundaries before apostrophe normalization. It SHALL distinguish contraction/possessive apostrophes from quotation delimiters. A quoted expression SHALL be opaque local material, and its internal pointing words SHALL NOT contribute pointing evidence. Empty lexical content SHALL NOT bypass the local-material veto. Shipped referential and follow-up rules and the newest-subject selection SHALL remain unchanged.

A quantified information-request phrase directly modified by a historical-subject complement SHALL NOT alone establish a local introduction. The relation SHALL be scoped to the requested nominal, not inferred from question syntax or a nearby preposition and pointer. An independently evaluated, created, possessed or presented nominal SHALL remain local material even when modified by a historical pointer. Other local material anywhere in the turn SHALL retain the whole-opportunity veto; the exemption SHALL NOT bypass title/content licensing.

A copular "it" whose complement is a bare time, date, clock, weather or distance expression SHALL be treated as dummy and SHALL NOT carry. A temporal phrase headed by `on`, `in`, `at`, `after`, `before`, `by`, `for`, `from`, `until` or `during` is an adjunct of a real referent and MAY carry when the content licence permits it. Closing idioms retain their exclusions, including "drop it"; losing a real keep-or-drop instruction is a disclosed recall trade.

Function words, pointers and clock numbers SHALL be tested by surface spelling after contraction splitting, including when they also appear in vault referential filler. Inflectional forms SHALL still apply to subject titles, frozen task vocabulary, the remaining referential vocabulary and neutral time words. A word whose stem merely matches a function word SHALL remain content. Each inline or fenced code span SHALL count as one unlicensed content word; a pointer inside it SHALL NOT point back. Inline backticks SHALL match exact delimiter runs. Markdown fenced blocks SHALL close with the same delimiter kind and a run at least as long as the opening run, including longer valid closing fences.

A content-free pointing turn MAY carry the newest subject within the one-third footprint. Its carry rate SHALL be reported separately from topic-switch false carries, which remain gated at at most five percent on sets not used for tuning. Recall losses SHALL be disclosed without gating. This round-seven ruling supersedes the round-four all-false-carry bar.

Length is not by itself a criterion: a long turn may carry when all its content is licensed.

**Precedence.** The conversation carry SHALL sit at one fixed place in the order in which activation decides a turn that its words did not resolve. That order builds on the keyless continuity, referential recency and follow-up contracts as they stand on `main` (context-activation-continuity, and memory-loop in `close-memory-loop`), and SHALL NOT change any of them. For each request, the first rule below that decides it wins:

1. An `anchor` override (`agent_choice`).
2. Resolution by the turn segment and the `focus` segment.
3. The referential recency rule, for a referential turn ("A turn that names nothing MAY resolve to the hottest recent anchor"), including the continuity-thread resume.
4. The follow-up carry from the caller's own session tier (`carried_by = "follow_up"`), for a turn that meets the shipped follow-up test.
5. The conversation carry, specified here.
6. The retrieval carry from one dominant recall hit, where memory-loop admits it.
7. Abstention.

A rule *decides* when it resolves or carries an anchor, or when it abstains `ambiguous`. A rule that finds nothing falls through to the next.

The conversation carry therefore runs before the retrieval carry. A licensed anaphoric turn whose subject the user named earlier is carried from that subject. Where the conversation carry abstains `ambiguous`, the retrieval carry SHALL NOT run. Unlicensed content falls through to shipped retrieval or abstention.

The conversation carry SHALL run for an anaphoric turn only when both hold:

- no earlier rule has decided it;
- the conversation has at least one visible `user` entry.

The compiler SHALL then walk the visible `user` entries of `recent` from newest to oldest. It SHALL stop at the first entry in which at least one anchor resolves under the ordinary rules applied to that entry's text alone, with no recall query and no embedding.

The compiler SHALL check the content licence against each selected anchor's own title. If any selected anchor fails the licence, the conversation carry SHALL fall through, without searching older entries for a different subject.

- **Exactly one anchor.** That anchor SHALL be carried as the packet's single anchor:
  - at status `partial`, with evidence `[conversation]` and `origin = "conversation"`;
  - its material served under the ordinary lanes, with all emitted conversation-inferred subject prose capped at `clamp_budget(max_chars) // 3`;
  - `generation.carried_by = "conversation"`.
- **Two or more.** The turn SHALL abstain `ambiguous` listing them, and SHALL NOT choose.
- **No entry resolves an anchor.** The turn SHALL abstain exactly as without `conversation`.

A refs-only conversation SHALL NOT carry: a ref says what was read, not what "it" refers to. A carried anchor, being `partial`, SHALL NOT enter the continuity token.

The inclusive subject charge SHALL count anchor and ambiguity titles, current-state statements, unit text, pointer titles/why and associated free authored strings such as provenance categories and unvalidated date/lifecycle strings, once per emitted occurrence under the existing character convention. References/paths and validated closed structural metadata MAY be exempt; arbitrary authored strings SHALL NOT be exempt solely because their field is labelled metadata. The same charge SHALL bound conversation-derived ambiguity and the no-material fallback. Ordinary carry and current-turn contact accounting SHALL remain unchanged.

The compiler SHALL budget `recent_context` exactly as for an unresolved turn, then full anchor/ambiguity titles in existing order, state entries, ordered units and overflow pointers. An unaffordable title SHALL become empty while its identity and evidence remain; the compiler SHALL NOT truncate it, drop an ambiguity candidate or choose a cheaper candidate. Complete prepared state/unit/pointer payloads SHALL fit their charged allowance or follow existing deferral/drop behavior. `budget.used_chars` SHALL equal the retained recent-context charge plus the retained inclusive subject charge and SHALL NOT exceed the effective request ceiling. Egress filtering SHALL reconcile the same charge after removal without refilling from withheld material.

#### Scenario: A conversation carry keeps within a third of the budget

- **WHEN** an unresolved pointing turn carries a conversation subject with more material than the request's `max_chars`
- **THEN** all emitted subject prose, including anchor titles and free authored metadata, uses at most `clamp_budget(max_chars) // 3` characters
- **AND** the anchor remains `partial` with evidence `[conversation]`
- **AND** `recent_context` retains the budget it receives for an unresolved turn
- **AND** the same subject reached by the turn's own words retains the ordinary budget

#### Scenario: A local task object is not the historical subject

- **WHEN** an earlier user entry resolved a subject and the current turn is "i have a new plan; can you review it"
- **THEN** the locally introduced plan vetoes the conversation carry even though every word has neutral or frozen-task treatment
- **AND** the compiler does not search an older entry for another subject

#### Scenario: A quantified request asks about the historical subject
- **GIVEN** a licensed historical subject
- **WHEN** the current turn is an elliptical request such as "any update on them"
- **THEN** its directly modified information-request nominal alone SHALL NOT establish local material
- **AND** the ordinary title/content licence SHALL still apply

#### Scenario: A historical modifier does not erase an independently evaluated object
- **GIVEN** a licensed historical subject
- **WHEN** the current turn asks "can you review a plan for it" or "is a plan with it ready"
- **THEN** the independently evaluated plan SHALL establish local material and veto the conversation carry
- **AND** an eligible historical-information request elsewhere in the turn SHALL NOT bypass a separate possession or presentation assertion

#### Scenario: A quoted task expression is local material

- **WHEN** the current turn asks whether an explicitly quoted task expression such as "they are ready" is correct
- **THEN** the quoted expression is local material and the conversation SHALL NOT carry
- **AND** the quoted pronoun supplies no backward-pointing evidence

#### Scenario: A locally supplied value choice stays local

- **WHEN** the current turn supplies two numeric or temporal alternatives and asks which of those is right
- **THEN** the local choice vetoes the conversation carry even though the alternatives are neutral vocabulary
- **AND** a historical pointer such as "the next one" or a temporal adjunct such as "on Tuesday" alone does not establish that local choice

#### Scenario: Long titles cannot escape the inferred subject allowance

- **WHEN** a real indexed subject's title exceeds the conversation allowance, including a no-material fallback or conversation-derived ambiguity
- **THEN** its returned title is empty, its identity remains available and the inclusive subject charge stays within the allowance
- **AND** ambiguity candidates remain present without selecting a cheaper candidate

#### Scenario: Post-egress accounting matches the retained inferred material

- **WHEN** egress removes charged conversation-inferred material
- **THEN** the reported charge accounts only for the remaining recent context and inclusive subject material
- **AND** the compiler does not refill from withheld material

#### Scenario: A new content word prevents the conversation carry

- **WHEN** an earlier user entry resolved a subject and the current turn is "i got a new offer; how should i respond to it"
- **THEN** "offer" is content even though an inflectional stem matches "off"
- **AND** the turn SHALL NOT carry that conversation subject

#### Scenario: Content-free inference is permitted within its footprint

- **WHEN** an earlier user entry resolved a subject and the current turn is "was that your final reply"
- **THEN** the newest subject carries with `generation.carried_by = "conversation"`, at `partial` with evidence `[conversation]`
- **AND** its served subject prose uses at most `max_chars // 3` characters

#### Scenario: Code pointers are opaque content

- **WHEN** an earlier user entry resolved a subject and the current turn is "can you explain `it = 1`" or contains a fenced code block
- **THEN** each code span counts as one unlicensed content word and its internal pointer does not point back
- **AND** the conversation SHALL NOT carry

#### Scenario: A rich follow-up keeps the conversation's subject

- **WHEN** an earlier user entry resolved the invented entity "Ottilie Marsh", and the current turn is "Given everything above, how did her results compare with the spring round, and should we change anything before the next one?"
- **THEN** "Ottilie Marsh" is carried as a single `partial` anchor with evidence `[conversation]` and `generation.carried_by = "conversation"`
- **AND** the same turn without `conversation` abstains `unresolved`

#### Scenario: The newest subject wins over an older one

- **WHEN** the newest user entry resolved "Kestrel hiring plan", an older one resolved "Harbor Lantern budget", and the turn is "what are the risks with that?"
- **THEN** "Kestrel hiring plan" is carried
- **AND** nothing from "Harbor Lantern budget" is served

#### Scenario: Shared earlier words do not license a conversation carry

- **WHEN** a pointing turn reaches no anchor, its content admits one dominant recall hit, an earlier user entry repeated those words but resolves a subject whose title does not license them, and they are not frozen task words
- **THEN** the conversation carry falls through to the shipped retrieval carry
- **AND** nothing is carried from that earlier subject

#### Scenario: A bare clock complement is dummy

- **WHEN** an earlier user entry resolved a subject, and the turn is "it's nearly midnight" or "is it Tuesday yet"
- **THEN** the copular "it" is dummy and nothing is carried from the conversation

#### Scenario: Preposition-headed time adjuncts preserve a real referent

- **WHEN** an earlier user entry resolved a subject, and the turn is "is it on Tuesday", "is it still on for Friday", "it is at noon", "is it after the weekend" or "is it the one from last week"
- **THEN** the subject is carried with `generation.carried_by = "conversation"`

#### Scenario: Drop it remains a closing

- **WHEN** an earlier user entry resolved a subject, and the turn is "drop it"
- **THEN** nothing is carried from the conversation, even following an explicit keep-or-drop choice

#### Scenario: A shipped follow-up carry still wins

- **WHEN** a short follow-up turn is carried from the caller's own session tier under the shipped follow-up rule, and the conversation's newest user entry names a different anchor
- **THEN** the packet reports `generation.carried_by = "follow_up"`, unchanged from today

#### Scenario: A numeral "one" is not an anaphor

- **WHEN** a turn says "Cheers, there is plenty of chat for one day." and an earlier user entry resolved an anchor
- **THEN** the turn is not anaphoric and nothing is carried

#### Scenario: A turn with new content is a topic switch

- **WHEN** an earlier user entry resolved an anchor, and the turn is "does it snow much in oslo in march", "is it possible to install Python 3.13 on my laptop?" or "it's been a long day"
- **THEN** the turn brings content unlicensed by the subject's own name/title or frozen task vocabulary, and nothing is carried from the conversation

#### Scenario: A contracted anaphor is carried

- **WHEN** an earlier user entry resolved "Ottilie Marsh", and the turn is "it's still on track for the autumn?"
- **THEN** "Ottilie Marsh" is carried with `generation.carried_by = "conversation"`

#### Scenario: A governed pointer is an anaphor

- **WHEN** a turn says "which one is cheaper?" or "the other one, please"
- **THEN** the turn is anaphoric

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

**Call ledger.** The call ledger SHALL record `conversation` by its name and byte length only, never a hash (call-ledger: "Arguments Are Recorded As Shape And Hash, Never Values", as modified by this change).

**Credentials.** Before anything is matched, the server SHALL run the shared egress scrubber over `focus` and every `recent` entry. A credential it recognises SHALL be removed, never matched, and its removal SHALL report `truncated`.

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
- **AND** no file, the call ledger included, contains its sha256 or the sha256 of the serialised `conversation` argument

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

The tool description of `activate_context` SHALL state, and the shipped skill scaffold SHALL echo:

- that in a longer conversation, or when the user's words lean on attachments, the agent also passes `conversation`, with `focus` one line naming the subjects now in play (including names or objects it read from attachments) and `refs` the pages it already read;
- the field bounds, and that every field is optional;
- that `turn` stays verbatim and is never rewritten or replaced by a summary;
- that `focus`-origin anchors are the agent's cues (`origin = "focus"`), not the user's words, and that activation reads no attachment itself.

The server instructions SHALL keep asking for the user's message verbatim as `turn` and SHALL carry only a short pointer to the tool description, within the 900-character bound the existing server-instructions test enforces. That bound SHALL NOT be raised. No surface SHALL ask the agent to summarise the conversation into `turn` or to paste whole earlier turns.

#### Scenario: Instructions point at the tool and stay short

- **WHEN** a client reads the server instructions at `initialize`
- **THEN** they still ask for the message verbatim and point at `conversation`
- **AND** they are at most 900 characters

### Requirement: Pre-registered conversation benchmark group

The context-activation audit SHALL add a group named `conversation`, authored and digest-pinned in a commit that precedes its first scored run. It SHALL use invented names only.

The group SHALL hold at least:

- twelve rich single turns of two to five sentences, at least four of which span two domains;
- twelve multi-turn conversations of three to six earlier entries plus a current turn;
- one negative twin per case: the same current turn with a conversation about unrelated invented material, or the same conversation with a current turn that reaches nothing;
- three drowning cases, where a long conversation about one subject is followed by a current turn about another;
- three topic-switch cases, where the newest subject is gold and the older one is poison;
- one withheld-versus-absent pair, scored for byte identity;
- two attachment cases, each a nearly content-free turn ("thoughts on this?") with a fixture-authored `focus` of cues as a vision layer would read them, gold on the named anchors with `origin = "focus"`, plus a twin whose `focus` cues name nothing in the corpus.

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

These bounds are the acceptance measure for this change. Live-cell end-to-end latency is owned by a separate lane. This change SHALL NOT regress it: a request without `conversation` SHALL do no conversation work, and SHALL record no `working_set.conversation` span.

A conversation stage that cannot start within the request's remaining reserve SHALL be skipped, and the packet SHALL then be compiled exactly as without `conversation` and report `generation.conversation = "absent"`. The skip SHALL NOT fail the request.

#### Scenario: A request without a conversation does no conversation work

- **WHEN** `activate_context` runs with `include_timings=true` and no `conversation`
- **THEN** its timings carry no `working_set.conversation` stage

#### Scenario: A spent deadline drops the conversation, not the packet

- **WHEN** the request deadline leaves less than the conversation stage's reserve
- **THEN** the packet is compiled without the conversation
- **AND** it reports `generation.conversation = "absent"`
