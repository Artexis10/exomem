## MODIFIED Requirements

### Requirement: An anaphoric turn is carried from the newest conversation subject

A turn SHALL be carried from the conversation only when all four of these hold:

1. **It resolves nothing of its own.** Its turn segment reaches no anchor by any worded contact kind.
2. **It points back.** It speaks a cue from the effective referential vocabulary, or one of a closed, shipped set: third-person pronouns and possessives; "that", "this", "those", "these", "it"; the follow-up markers already shipped, except the bare pointers "one", "ones" and "other"; an ordinal followed by "one" or "option"; "the former" and "the latter"; an elliptical "what about …" or "how about …" opener. A bare pointer counts only when a determiner ("the", "a", "an"), a demonstrative, an ordinal or "which" governs it ("that one", "the other one", "which one"); a numeral use ("for one day", "no one") does not. Contractions are split first ("it's" is "it is"). A demonstrative that places a time ("this week", "these days") does not point back.
3. **Its content has literal support from the subject.** Every remaining content term SHALL match the selected subject's admitted title or one individual admitted semantic unit in its existing compiler neighbourhood, using the existing lexical normalization. The system SHALL admit pages and units before matching, ordering or limiting support. Withheld text, including text removed with a notice, SHALL supply no support. It SHALL NOT union words across units or earlier turns, use a frozen task vocabulary, or select a different subject from content overlap. Missing support SHALL fall through without declaring the user's intent. Existing neutral and temporal treatment SHALL remain unchanged.
4. **It supplies no turn-local material.** Before neutral-word removal or title licensing, the compiler SHALL veto the conversation carry when the current turn structurally introduces a nominal/value, quotes an explicitly delimited expression, or supplies an alternative set that a selecting pointer addresses. Content, time, numeric and neutral words SHALL remain eligible heads for this check. A pointer-headed historical expression or temporal adjunct SHALL NOT alone establish an introduction; coordination alone SHALL NOT establish a local choice. A detected local-material class SHALL veto the entire conversation-carry opportunity, including mixed local/historical turns, without attempting general coreference.

The local-material check SHALL retain surface clause/list and quotation boundaries before apostrophe normalization. It SHALL distinguish contraction/possessive apostrophes from quotation delimiters. A quoted expression SHALL be opaque local material, and its internal pointing words SHALL NOT contribute pointing evidence. Empty lexical content SHALL NOT bypass the local-material veto. Shipped referential and follow-up rules and the newest-subject selection SHALL remain unchanged.

A quantified information-request phrase directly modified by a historical-subject complement SHALL NOT alone establish a local introduction. The relation SHALL be scoped to the requested nominal, not inferred from question syntax or a nearby preposition and pointer. An independently evaluated, created, possessed or presented nominal SHALL remain local material even when modified by a historical pointer. Other local material anywhere in the turn SHALL retain the whole-opportunity veto; the exemption SHALL NOT bypass title/content licensing.

A copular "it" whose complement is a bare time, date, clock, weather or distance expression SHALL be treated as dummy and SHALL NOT carry. A temporal phrase headed by `on`, `in`, `at`, `after`, `before`, `by`, `for`, `from`, `until` or `during` is an adjunct of a real referent and MAY carry when the content licence permits it. Closing idioms retain their exclusions, including "drop it"; losing a real keep-or-drop instruction is a disclosed recall trade.

Function words, pointers and clock numbers SHALL be tested by surface spelling after contraction splitting, including when they also appear in vault referential filler. Inflectional forms SHALL still apply to admitted subject titles and supporting units, the remaining referential vocabulary and neutral time words. A word whose stem merely matches a function word SHALL remain content. Each inline or fenced code span SHALL count as one unlicensed content word; a pointer inside it SHALL NOT point back. Inline backticks SHALL match exact delimiter runs. Markdown fenced blocks SHALL close with the same delimiter kind and a run at least as long as the opening run, including longer valid closing fences.

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

The compiler SHALL check literal support for each selected anchor independently. If any selected anchor lacks support, the conversation carry SHALL fall through without searching older entries or choosing only the candidate whose material matches. A successful single-anchor carry SHALL retain its required supporting unit in the bounded final packet. Ambiguity SHALL retain the existing candidate-only response without emitted units. If a single carry's witness cannot fit before return, compilation SHALL fall through. If terminal disclosure removes that witness, the response SHALL use existing abstention without a conversation-carry claim or recursive compilation.

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
- **THEN** the locally introduced plan vetoes the conversation carry regardless of its words' neutral treatment or literal support
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

- **WHEN** an earlier user entry resolved a subject and the current turn is "what about her"
- **THEN** the newest subject carries with `generation.carried_by = "conversation"`, at `partial` with evidence `[conversation]`
- **AND** its served subject prose uses at most `max_chars // 3` characters

#### Scenario: Code pointers are opaque content

- **WHEN** an earlier user entry resolved a subject and the current turn is "can you explain `it = 1`" or contains a fenced code block
- **THEN** each code span counts as one unlicensed content word and its internal pointer does not point back
- **AND** the conversation SHALL NOT carry

#### Scenario: A rich follow-up keeps the conversation's subject

- **WHEN** an earlier user entry resolved the invented entity "Ottilie Marsh", one of her admitted units supports every content term, and the current turn is "Given everything above, how did her figures look before the next one?"
- **THEN** "Ottilie Marsh" is carried as a single `partial` anchor with evidence `[conversation]` and `generation.carried_by = "conversation"`
- **AND** the packet serves the supporting unit
- **AND** the same turn without `conversation` abstains `unresolved`

#### Scenario: The newest subject wins over an older one

- **WHEN** the newest user entry resolved "Kestrel hiring plan", an older one resolved "Harbor Lantern budget", the newest subject has an admitted unit supporting "risks", and the turn is "what are the risks with that?"
- **THEN** "Kestrel hiring plan" is carried
- **AND** nothing from "Harbor Lantern budget" is served

#### Scenario: Shared earlier words do not license a conversation carry

- **WHEN** a pointing turn reaches no anchor, its content admits one dominant recall hit, an earlier user entry repeated those words but resolves a subject whose title and admitted units do not support them
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

- **WHEN** an earlier user entry resolved an anchor whose admitted title and units supply no literal support, and the turn is "does it snow much in oslo in march", "is it possible to install Python 3.13 on my laptop?" or "it's been a long day"
- **THEN** the selected subject's admitted title and units supply no literal support, and nothing is carried from the conversation

#### Scenario: A contracted anaphor is carried

- **WHEN** an earlier user entry resolved "Ottilie Marsh", and the turn is "it's still on for the autumn?"
- **THEN** "it's" is read as "it is", which points back
- **AND** "Ottilie Marsh" is carried with `generation.carried_by = "conversation"`

#### Scenario: A governed pointer is an anaphor

- **WHEN** a turn says "which one is cheaper?" or "the other one, please"
- **THEN** the turn points back
- **AND** any content word it has still needs literal support before it can carry

#### Scenario: A turn that names its own subject is not carried

- **WHEN** a turn uses a pronoun but reaches an anchor by its own words
- **THEN** no carry runs, and the conversation contributes only the `conversation` qualifier

#### Scenario: Admitted material supports a title-missing follow-up

- **WHEN** the newest user subject resolves and one admitted semantic unit supports content absent from its title
- **THEN** the conversation carry retains that subject and supporting unit within its existing footprint
- **AND** it adds no task-word exemption or alternative subject

#### Scenario: Withheld support is indistinguishable from absent support

- **WHEN** otherwise identical vaults differ only in a withheld unit containing the follow-up's content
- **THEN** their conversation carry, fallback and observable support results remain equivalent
- **AND** a notice-level removal supplies no lexical support

#### Scenario: Several fragments cannot manufacture a witness

- **WHEN** required content occurs only across several different units or unrelated linked pages
- **THEN** the conversation carry finds no single-unit witness and falls through
- **AND** it does not infer support by unioning those fragments

#### Scenario: Literal support does not disambiguate the subject

- **WHEN** the newest user entry resolves several anchors and only one has literal support
- **THEN** the conversation carry falls through under its all-candidates support rule
- **AND** content overlap does not select that anchor

#### Scenario: The witness cannot survive the packet budget

- **WHEN** a successful single-anchor carry's required supporting unit cannot fit before the compiler returns
- **THEN** the conversation carry falls through
- **AND** it does not return an anchor whose support exists only in discarded material

#### Scenario: Terminal disclosure removes the witness

- **WHEN** final disclosure removes a successful single-anchor carry's required supporting unit
- **THEN** the response uses existing abstention without a conversation-carry claim
- **AND** it neither keeps the unsupported anchor nor recursively recompiles against changed authority
