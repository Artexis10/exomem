## Context

The rule in `AGENTS.md` is that every category set lives in a vocabulary registry. Code branches only on closed attribute values that a registry schema declares and code implements (`family`, `role`, status `class`). It never branches on an entry key.

Relation types follow the rule in part. They are registry entries, and a namespaced extension must name one core parent and inherit its family (`family_mismatch`). But code branches on particular family values and relation keys. Kinds and verdicts do not follow the rule at all.

This change applies the rule to the whole epistemic language. For relation words it narrows the rule: code reads behaviours, never a family value (see "The rule for every inventory entry").

## Decisions

### Three layers: words, behaviours, dimensions

- **A word** is a registry entry: a relation type, a kind, a verdict, a modality, a belief level, an inference mode. It has a key, labels per language, a description and examples. It keeps its own meaning. `refines` and `contradicts` both bear on a claim, but they say different things and stay distinct entries.
- **A behaviour** is something code does. The registry schema declares each closed set, and code implements each member. Adding one is a code change and a schema change, and that is the point: a behaviour is a promise code keeps. There are three sets, because each has its own target: relation behaviours, kind traits and dimension display behaviours.
- **An epistemic dimension** groups words by the question they answer. Dimensions are registry data, and a vault can add one.

**Why this split:** the owner asked whether the language can be fully dynamic. It can be dynamic in everything except what code does with a meaning. Making behaviours data too would mean code that runs arbitrary rules from a registry, and that turns a vocabulary into a programming language. Small closed sets keep the code honest, and every word above them stays free.

### Relation behaviours live on core relation entries

A core relation entry gains a `behaviours` list, beside its existing `presentation_role`. An extension inherits its parent's behaviours and cannot set its own, just as it inherits the parent's family. Vaults do not create families.

Behaviours sit on relations, not families, because today's sets are keyed both ways. The two members of an inverse pair can need different behaviours: the graph stores each edge as written, so `precedes` and `follows` carry opposite order behaviours, and code never has to ask which key it holds. Each behaviour replaces one set.

Table rows cite the 2026-11-08 language inventory by ID: F for a branch on a family value, K for a branch on a relation key, C for a branch on a kind. Line numbers refer to `afe4bffdd`.

| Behaviour | What code does | Replaces (inventory) | Core relations |
|---|---|---|---|
| `lifts` | A unit relation may become a page-level proposal. | F1 `_LIFT_RELATION_FAMILIES` | answers, resolves, raises_question, supports, contradicts, refines, evidenced_by, duplicates |
| `membership` | The catalogue reads the edge as the source belonging to the target. | F2 `MEMBERSHIP_FAMILY` | member_of |
| `bears_on` | A relation authored on a prediction or question unit counts toward settling it. Inbound edges and edges from elsewhere on the page do not. | K2 `_PREDICTION_RESOLVING_RELATIONS`, `_QUESTION_ANSWERING_RELATIONS` | supports, contradicts, resolves, evidenced_by |
| `settles` | The edge settles its target. The graph pairs pages that settle one target, and stance treats such pages as a declared pair. | K1 `_RESOLUTION_RELATION_TYPES`; K17 `ANSWERS` | answers, resolves |
| `sources` | The edge is provenance. Activation counts it, and entity hydration never treats it as a content link. | K3 `_PROVENANCE_RELATIONS`; K12 `derived_from` exclusions | derived_from, evidenced_by, cites |
| `replaces` | The target is no longer the current view. Currency, role review, the sensed model and census chains follow the edge. It is the only behaviour through which a frontmatter edge qualifies in relation review. | F5, F6, K5, K6, K9 (chain), K11 | supersedes |
| `corrects` | An edge into a unit surfaces its source as a counterpart beside the unit. An episode write must pin the edge's exact current target. | F3 `CORRECTIVE_FAMILIES` | contradicts, refines, supports, supersedes, resolves, answers |
| `associative` | Relation review never counts the edge as a typed edge, because it records a link, provenance or a mention, not a claim about the target. | F4 `_EXCLUDED_FAMILIES` (drops the dead `provenance`) | links_to, cites, derived_from, evidenced_by, mentions, observed_in |
| `plain_link` | The edge is a plain link. It ranks after typed edges, never counts as typed, and the census skips it. | F10, F11, K16 readers (projection_runtime 1825, vocabulary_projection 164, relation_census 64/902/1542) | links_to |
| `opposes` | Source and target are rivals. Stance reads the pair as declared, the census flags it inside one supersession chain, and the audit raises it as an asserted contradiction. | K17 `CONTRADICTS`, K9 | contradicts |
| `names_subject` | The edge names the entity a unit is about. Restricted working-set admission matches it against the turn's subject. | K20 | about_entity |
| `earlier_than_target` | The source comes before the target in time. | new | precedes |
| `later_than_target` | The source comes after the target in time. | new | follows |

An extension's behaviours resolve through its core parent, never through a saved family. `assesses`, `precedes` and `follows` carry no behaviour beyond the order pair. Relation review counts all three as typed edges, because none is `associative`.

`presentation_role` stays renderer metadata: it tells a viewer how to draw an edge. Behaviours decide what code does with the edge. `supersedes` carries both, for those two different reasons. `presentation_role` cannot carry a promise, because an extension may set and later change it.

An inventory of every branch on a relation key or family value in `src/exomem` maps each branch to a behaviour, a binding or named debt, under the rule below. Extensions change on purpose: the intended-changes list names each change, and the fixture shows each one.

### The rule for every inventory entry

1. A reader whose set decides what a user or agent gets becomes a behaviour or trait read.
2. A census-only rule becomes a behaviour read only when an adopted behaviour already covers its set. Otherwise it stays as named debt.
3. A producer that writes one core key reads a registry binding when one exists, or when its copies must fold. Otherwise it stays a named producer constant.
4. Labels, teaching copy and frozen instrument schemas stay as named debt.
5. Code never names a family value. It branches on behaviours, traits, `role` and status `class`. Family stays meaning data that dimensions and traversal profiles read.

Rule 5 narrows the `AGENTS.md` wording "`family`, `role`, status `class`" for relation words. Family is the grouping that dimensions and profiles read, so a family read couples code to display grouping. Inverse pairs and cross-family sets (`settles`, `corrects`) need per-relation statements anyway. Two mechanisms for one concept caused the drift that the inventory found: F6 reads supersession by family, and K5 reads it by key.

Interfaces:
- `RelationRegistry.behaviours_of(key)` returns the entry's own behaviours for a core key and the parent's for an extension.
- `RelationRegistry.keys_with(behaviour)` returns core and extension keys.
- `RelationRegistry.frontmatter_fields()` and `frontmatter_relation(field)` expose the field binding (see "Frontmatter fields are registry data").
- The kind registry gets `traits_of(kind)` and `kinds_with(trait)`.
- SQL readers ask `exomem_edge_behaves(EDGE_COLUMNS, behaviour)`, which reads the edge's behaviours through its authoring page's registry. A reader that filters on stored keys expands them through `traversal_profiles.relation_query_plan`, as `relation_edges()` does, or keeps the candidate status, because an extension is stored as a candidate.

### Inventory decisions

1. **`replaces` has three shapes.** One `replaces` behaviour on `supersedes`, inherited through the parent, replaces the family shape (F5, F6) and the key shape (K5, K6, K11 and the chain half of K9). The frontmatter pointer shape is not a relation read: `supersedes` and `superseded_by` are lifecycle fields that `frontmatter_fields` binds to `supersedes`, so a graph edge from either field carries `replaces`. K21's pointer readers keep the field tokens, and the rule they repeat is debt.
2. **`lifts` is a per-relation behaviour** on the eight keys in the table, inherited through the parent. Core relations, and every extension whose family equals its parent's, do not change. A grandfathered extension with a different saved family now follows its parent. The pack keeps the causality reason as a YAML comment beside `causes` and `caused_by`: lifting one unit's causal claim would assert a mechanism between pages.
3. **Key-set mismatches.** Stance reads `settles` as well as `opposes`, so two pages that both `resolves` one target become a declared pair, as two pages that both `answers` one target already are. The graph already pairs pages that settle one target. If dogfood shows near-duplicate pairs that both resolve one target, stance reverts to `answers` only. The dreamer's content-link exclusion (K12) widens to the `sources` behaviour on purpose. K10, K13 and K14 mean "the Sources a page declares": they select by the `sources` field and take the relation from `frontmatter_relation("sources")`. There is no `declared_source` behaviour. The shared-source candidate copies in `epistemic_graph` and `relation_queue` stay separate; folding them is debt, because it changes suggestions.
4. **`frontmatter_fields` is a core-only relation attribute** that maps a field name to `forward` or `reverse`. The loader derives the `frontmatter` origin from it. `origins` says which producers may emit a relation, and `frontmatter_fields` refines the frontmatter producer to field and direction. Wikilink, semantic-unit and semantic-block producers need no binding, because their origin names the relation. They stay producer constants (debt). A vault cannot bind its own field yet.
5. **Kind traits gain `question`**, shipped on `open_question`. It folds the two differing sets C6 and C7, and the graph's shared-open-question generator reads it. `evidential` stays with its one census check as debt, and `comparable` is not added, because it would tie the frozen `units-v1` instrument to vault data. `closable` is declared in slice 5 with `flag_open`. C9 stays on category, because a unit's category and kind diverge when it carries an explicit `category:` row.
6. **Proposed relation behaviours.** Adopt `corrects`, `associative` (renamed from `plain_association`, so it does not read as a twin of `plain_link`), `plain_link`, `opposes` and `names_subject`. Reject `declared_source` (the concept is the `sources` field), `target_expects_evidence` and `target_expects_question` (census-only, and `target_kinds` is a hard scope that would drop edges from the graph), and `marks_duplicate` (a producer default that writes one core key). Keep `cross_page_only` and the affiliation count (F8) as debt, because each has census readers only.
7. **The four shipped traversal profiles move** into `src/exomem/vocabulary/packs/core/traversal-profiles.yaml`, in the vault overlay's grammar, with today's priority (sorted families). `all` stays computed from the core families, and the new families `order` and `assessment` join only `all`. Profiles carry direction, priority and depth, which dimensions lack, so they are not derived from dimensions.

### Frontmatter fields are registry data

| Relation | `frontmatter_fields` | `origins` written in the pack (the loader adds `frontmatter`) |
|---|---|---|
| derived_from | `sources: forward` | semantic_relation, semantic_block, semantic_unit |
| evidenced_by | `evidence: forward`, `evidences: forward`, `evidence_paths: forward` | semantic_relation |
| supersedes | `supersedes: forward`, `superseded_by: reverse` | semantic_relation |
| links_to | `related: forward` | semantic_relation, wikilink |

Rules:
- Only the core pack sets the attribute. It joins `_CORE_FIELDS`, not `_DEFINITION_FIELDS`.
- A field is bound to one relation, and its direction is `forward` (the page is the edge's source) or `reverse` (the page is its target).
- The loader adds `frontmatter` to the bound relation's `origins`. A core entry that writes `frontmatter` in `origins` itself fails the pack build.
- Readers: graph indexing (`_page_level_edges`), relation facts (`_derive_relation_facts`) and activation (`measure_document`). Activation counts a field as typed when its relation lacks `plain_link`.

### Kind traits are a separate closed set

Kinds need their own set, because a relation behaviour has a source and a target and a kind has neither. A kind entry gains a `traits` list:

| Trait | What code does | Replaces (inventory) | Shipped kinds |
|---|---|---|---|
| `assertion` | Activation counts the unit as an assertion. `inference_mode` is valid only on such a unit (slice 4). | C2 `_ASSERTION_BLOCK_TYPES` | claim, finding, inference, hypothesis, result |
| `page_claim` | The claims reader takes the first such unit as the page's claim. | C3 | claim |
| `procedure` | Role review reads the unit as a procedure. The category fallback stays as debt. | C4 kind leg | procedure |
| `outcome` | Role review reads the unit as an outcome. The category fallback stays as debt. | C5 kind leg | result |
| `question` | The graph's shared-open-question generator and the census read the unit as a question. The category leg stays as debt. | C6 kind leg, C7 | open_question |
| `closable` | `flag_open` marks the unit while no verdict or answer exists. Declared in slice 5 with `flag_open`. | new | open_question, hypothesis, prediction, assumption |

A trait never schedules a check. `check_by` already does that on any unit, so a trait for it would duplicate the governed key.

The 27 built-in kinds in `semantic_blocks.py` move into the shipped pack. `definition` and `procedure` are among them already. The kind debt is in the debt table.

### Verdicts are state

Verdicts become a registry with shipped defaults (abandoned, confirmed, inconclusive, qualified, refuted), labels and descriptions. A vault can add a verdict or translate one. An experiment page's `outcome:` shares the verdict vocabulary today through one constant, so it follows the registry too: a vault-added verdict is a valid outcome. Outcomes take keys only, and unit verdicts also accept labels, which resolve to the same keys. Verdicts carry no behaviours. A verdict changes no lifecycle, standing or rank, as today, and a refuted claim keeps active standing.

### Epistemic dimensions

A dimension entry has a key, labels, a description, its members and its display behaviours. Members are relation families, unit kinds and governed metadata keys. One member may sit in several dimensions, because a word can answer more than one question. A unit belongs to a dimension when its kind is a member, when it carries a member key, or when it is an endpoint of an edge whose family is a member.

**Every dimension gets the generic operations:** retrieval filters by it, the context compiler groups by it, and reviews and reports queue by it. No code names a particular dimension.

**A dimension may also name display behaviours** from a closed set:

- `lead_with_current`: put current members before replaced ones, and mark the replaced ones;
- `show_origin`: show beside each member where it came from and whose it is;
- `flag_open`: mark members whose kind has the `closable` trait and that have no verdict or answer yet. An answer is an inbound edge whose relation carries `settles`;
- `order_in_time`: present members in time order, read from edges with `earlier_than_target` or `later_than_target` and from `occurred_at`. It is the only rule that orders events.

A display behaviour applies only within its own dimension's group. When a request groups by several dimensions, a unit that belongs to more than one appears in full in the first requested dimension and as a ref in the others, so orders cannot conflict.

Display behaviours change only order and annotation in compiled context. They never hide a unit, change its standing or change what a word means. When the budget is short, a replaced unit becomes a ref, as context activation already allows. A dimension change takes effect only after a reviewed registry save, as every registry change does.

The shipped dimensions:

| Key | Question | Members | Display |
|---|---|---|---|
| `currency` | Is it still current? | supersession | `lead_with_current` |
| `correction` | Was it corrected? | contradiction, testing, `verdict` | none |
| `provenance` | Where did it come from? | derivation, evidence, citation, observation | `show_origin` |
| `attribution` | Whose is it? | `asserted_by`, `credence_holder` | `show_origin` |
| `kind` | What kind of statement is it? | every kind, `modality`, `inference_mode` | none |
| `argument` | What is the case for and against? | support, contradiction | none |
| `dependency` | What does it rest on? | dependency, causality, blocking, implementation, mitigation, use, production | none |
| `entities` | What and whom is it about? | entity, membership, kinship, affiliation, ownership, operation, supply, location, composition | none |
| `identity` | Is it the same thing? | duplication, specialization | none |
| `scope` | Where does it hold? | refinement | none |
| `uncertainty` | How sure are we? | question, answer, resolution, assessment, kinds belief, hypothesis, assumption, prediction, open_question and risk, `check_by`, `credence` | `flag_open` |
| `order` | When did it happen? | order, kind timeline_event, `occurred_at` | `order_in_time` |

The generic families mention, link and relation belong to no dimension.

### Degree of belief, owned by its holder

The owner chose this shape on 2026-11-08: belief levels, plus numbers only with a source. The owner added that Exomem must not arbitrate belief.

1. A belief is a unit of the new kind `belief`. It states the belief in prose, so it is never an empty unit. Its governed keys are `credence_holder` (a plain name or a link to an entity page), `credence` (the value) and `credence_basis` (free text).
2. The unit points at the claim it assesses through the new core relation `assesses`, family `assessment`.
3. The value is a key or label of the belief-level registry (speculative, plausible, likely and near-certain by default), or a probability from 0 to 1.
4. Every belief names a holder. A probability also needs a basis, and the basis can be anything the holder gives, including intuition.
5. Exomem keeps the stated value as written. A label resolves to its level key for matching, but the stored text is unchanged, and a probability is never rounded.
6. Several holders' beliefs about one claim sit side by side, and none replaces another.
7. A `credence`, `credence_holder` or `credence_basis` row on another kind is an error that shows the belief-unit form. The likely misuse is `- credence: 0.8` on a claim.

A later change can score calibration from a probability, the assessed prediction's `check_by` and its verdict. This change only keeps the data that needs.

`credence_holder` names whose belief it is. `asserted_by` names whose claim a unit is. They are different keys, because the two questions differ: a vendor asserts an uptime figure, and the team believes it is likely wrong.

Exomem writes no belief in this change. A later change that computes one must label it derived and must not replace a holder's belief.

The rejected alternatives were levels only, which keeps the old rule but gives up calibration, and free numbers on any claim, which is the most exposed to unsupported precision.

### New governed metadata keys

- `asserted_by` is a plain name or a link to an entity page. Context presents a unit with it as that party's claim.
- `modality` takes a modality-registry key: is, should, must, may. Retrieval and context never merge units whose modalities differ.
- `occurred_at` is a date or a date and time on a `timeline_event`.
- `quantity_value`, `quantity_unit` and `quantity_error` record one measured quantity. Records remain the place for a series.
- `inference_mode` takes an inference-mode registry key on a unit with the `assertion` trait.

Every new key name is chosen to avoid ordinary rows: `value`, `at`, `holder`, `basis` and `mood` are common words in existing notes, and `credence`, `occurred_at` and `modality` are not.

`credence` is a unit row on a belief unit, never a frontmatter field. The canonical rule that confidence is not a frontmatter field still holds. Today's teaching says Exomem keeps no credence or numeric confidence anywhere. Slice 6 rewrites those sites: the scaffold's frontmatter, skill and vault-care references, `judge_categorically` in `commands.py`, and the comment beside the verdict set in `semantic_units.py`.

Each new key is reserved, as `verdict` and `check_by` are. A page that already uses one as an ordinary row gets a warning, not an error, for one release. The warning names the repair: rename the row, or give it a valid value.

### Multilingual labels

Each registry entry carries `labels: {language: text}`. Keys stay language-neutral. Parsing accepts a label as an alias of its key, so `## Väite` and `## Claim` are one kind. The registry check refuses a label that collides with another entry's key or label.

## Debt

Each item stays as named debt, with its reason in a code comment (task 2.4).

| Item | Sites | Why it stays |
|---|---|---|
| F7 same-page epistemic flag | relation_census 76, 1460 | Census-only. It is family-keyed, so extensions already reach it. |
| Projected graph lane tier (K16, change 6) | projection_runtime 1825 | A stored projection edge keeps its canonical key and no extension parent, so only a core plain link ranks last. An extension of `links_to` ranks as typed here until the producer stores the parent. |
| Shared-source copy in the relation queue (K14 fold) | relation_queue 700 | It reads the frontmatter field's stored edges only, while the graph copy also reads page-level extensions. Folding the two changes suggestions and needs its own evidence. It reads the field and relation from the binding. |
| F8 affiliation count | relation_census 1232 | One entity-census count for one family. |
| K7 evidence-target check, with C8 `_EVIDENTIAL_KINDS` | relation_census 74, 1447 | Census-only. `target_kinds` cannot express it, because folders and Sources pages count. The kind list stays with the check. |
| K8 answers-target check (relation side) | relation_census 1454 | Census-only. `settles` is wrong here, because a `resolves` target is often not a question. The target side reads the `question` trait. |
| K18 asserted-contradiction label | audit 7057 | A label on the finding. The pairs come from `opposes`, and an extension is labelled by its parent. |
| K19 duplicate default | adoption_proposals 448, 1327 | A producer default that writes one core key. A vault cannot remove a core key. |
| Wikilink producers and their authored-match checks | semantic_contract 4645; epistemic_graph 9046, 9312, 12361, 14135, 14923; relation_queue 661; governance/graph_producer 79 | Producer constants. `origins: [wikilink]` already states the fact. Convert them through one `origin_relation()` accessor when a second core relation lists that origin. |
| Drifted shared-source copy | epistemic_graph 14156 | It reads the binding's relation from any origin, while the `relation_queue` copy reads the field only. Folding them changes suggestions, so the fold needs its own evidence. |
| K21 pointer readers | find_types 202, 209; working_set 480, 1163, 2671; stance 77; delete_file 230; replace 436; context_pack 365, 673; memory_context 135; audit 6229, 6370; semantic_contract 4805; working_set_currency 34 | Naming the field tokens is sound. The repeated rule "a successor pointer means not current" is debt. Fold it into one lifecycle predicate that reads the reverse field from the `supersedes` binding. |
| K22 field-name lists | lexstore 584; episode_capture 54; review_context 37; entity_types 76; governance/bridges 60; governance/egress 812, 856, 5138; working_set_conversation 143; memory_context 136 | Transport and allow-lists. They carry no behaviour. |
| K24 teaching copy | commands 1600-1608; relation_vocabulary 84 | Prose. Slice 6 teaches from the pack. |
| C4 and C5 category fallbacks; C6 category leg; C9; C11; `CONCLUSION_CATEGORIES`; `_CATEGORY_BY_LABEL` | artifact_role_review 84-92; epistemic_graph 14748; audit 6018; context_roles 796; working_set_index 291 | Category registry, out of scope per the proposal. C9 stays on category for the reason in inventory decision 5. |
| C10 `UNITS_V1` | sensing 245-275 | Frozen instrument (proposal). |
| C12 `RECOGNIZED_SECTIONS`; C13 `PACK_ALLOWED_PRIMITIVES` | context_pack 101; knowledge_packs 62 | Heading text (proposal). C13 is a closed manifest schema that mixes page types and kinds. |
| Role-review verdict-key sets (`retracted`, `false`, `failed`) | artifact_role_review `_result_subject`, `detect`; artifact_role_state `_resolved` | They decide which verdict keys withdraw an artifact's result. None is a shipped verdict, so the legs were dead until a vault adds one. A verdict carries no behaviour, and a registry read needs a decision on a verdict attribute first. |
| Vault verdicts in the vault-neutral parse | semantic_index `current_parent_index_state`; lexstore `unit.verdict` column; the `unit` an `observe_memory` result returns | These read the parse that uses the shipped registry, as vault kinds and categories already are there. A vault verdict shows in `metadata.verdict` and in the selected unit view, not in `unit.verdict` of the neutral parse. A vault-registry change does not rebuild the derived stores, as for relations. |
| Page-type keys found on the way | vocabulary_projection 78; dreamer_families 615; artifact_role_state 207 | Note-type keys where the note-type `role` states the fact. Outside this change. |

Sound, not debt: K23 (NLI output labels, a closed model schema), the `generic_relation_key()` readers, and projection_runtime 1837 (a core-or-extension test).

## Intended behaviour changes

Each line needs a fixture case.

Extensions, through the parent:
1. An extension of `supports`, `contradicts`, `resolves` or `evidenced_by` clears a due prediction or open question (K2).
2. An extension of `derived_from`, `evidenced_by` or `cites` counts as activation provenance (K3), and entity hydration skips it (K12).
3. An extension of `answers` or `resolves` feeds `shared_resolution_target` candidates (K1).
4. An extension of `supersedes` reaches three readers: role review collects its target (K5), the census builds its chain and date check (K6), and the sensed model records it (K11).
5. The census checks an extension of `contradicts` inside a supersession chain (K9). Stance and the audit already reach it.
6. An extension of `links_to` ranks as a plain link in referent resolution and the vocabulary pair query, and the census skips it (K16 readers, F10 key leg). The graph's `typed` already treats it this way. The projected graph lane is excluded (see Debt).
7. An extension or alias of `about_entity` names the subject in restricted working-set admission (K20).
8. A page-level extension of `derived_from` seeds shared-source candidates. It also stops a frontmatter source from being proposed again as an authored relation (K14, through the binding).
9. A grandfathered extension whose saved family differs from its parent's follows the parent in every converted family read: lifts, corrects, associative, membership, replaces, plain_link.

Widened sets, core relations:

10. Stance treats two pages that both `resolves` one target as a declared pair. Near-duplicate and overlap warnings stop for that pair (K17).
11. A `cites` or `evidenced_by` edge into an entity page stops counting as a content link. That covers hydration targets, contributors and authored links (K12).

Kinds:

12. A vault kind with the `question` trait joins shared-open-question candidates and satisfies the census `answers` target check. The census drops its literal `question` (C7). The fixture shows that the plural and synonym headings still resolve to `open_question`.

## Fixture

Tasks 2.6 and 3.2 use one fixture vault, and compare main's code with the converted code.

Core parity: these outputs must be identical.
- `suggest_relations`: `unit_relation_lift`, `shared_resolution_target`, `shared_open_question`, frontmatter-source and shared-source candidates. Also the review batch's lift and source rows.
- Catalogue membership answers.
- Audit `prediction_window` and question aging.
- Activation counts (typed, provenance, assertion, frontmatter links) on a page that uses all seven frontmatter fields.
- `qualify_relation` reasons, including a frontmatter `superseded_by` pair and a mutual-supersession target.
- Unit-correction counterparts, and the `observe_memory` exact-target guard.
- Working-set currency.
- Role review: supersession targets, procedure and outcome.
- Sensed-model page facts.
- Census rows and every census check.
- Referent and projection tiers, for one plain link and one typed edge.
- The page claim.
- The built-in traversal profiles.
- For each frontmatter field, the graph and the relation facts record the same edge. Activation counts the relation per field and records no edge direction.

Extensions: one per parent, each showing its line in the intended-changes list.
- `x.backs` (supports), `x.based_on` (derived_from), `x.closes` (resolves), `x.retires` (supersedes), `x.disputes` (contradicts), `x.see_also` (links_to), `x.concerns` (about_entity).
- One grandfathered extension with parent `relates_to` and saved family `support`.
- Control: `x.qualifies` (refines) lifts before and after.

Widened sets:
- Two pages that both `resolves` one target.
- A compiled page that `cites` an entity page.

Kinds:
- A vault kind `research_question` with the trait `question`.
- A vault kind `protocol` with the trait `procedure`.

## Migration path

1. Land the registry plumbing (tasks 2.2 and 2.3) with no reader change. Record the fixture baseline on main first.
2. Convert the readers one module group at a time. Each conversion keeps the core parity outputs and flips exactly its own intended-change lines.
3. Release as one batch. Registry schema reads now show `behaviours` and `frontmatter_fields`, so regenerate the derived artifacts: the capabilities doc, the hosted render and the plugin tree.

## Risks

- **A behaviour change hides in a data migration.** For core relations, the shipped pack must reproduce today's sets, except two named widenings. The before-and-after fixture test guards each cutover.
- **Agents misuse the new words.** Each slice proves on real models that an agent picks the right word for realistic and hostile inputs.
- **Unsourced numbers creep in.** Validation refuses a probability without a holder and a basis, with an error that names the missing key.
- **Display annotations add noise.** `flag_open` marks every open question, hypothesis, prediction and assumption without a verdict. Slice 5 checks on real models that the annotations help, and f38's P arm shows their effect on outcomes.
- **Belief units add authoring weight.** A separate unit costs more than a row on the claim. In return, each belief keeps its holder, and several holders can disagree on record.
- **Behaviour keys from `core_registry()` alone miss vault extensions in SQL.** A stored key column holds core keys, and an extension is a candidate that each page interprets. Readers ask `exomem_edge_behaves`, or expand the keys through `relation_query_plan` over the admitted instances.
- **A non-core key means what its authoring page's instance defines.** A behaviour lookup uses that page's registry, as the census does.
- **The grandfathered family-mismatch case flips silently.** The fixture holds one such extension.
- **A partial cutover leaves one concept on mixed family and key reads.** The fixture asserts extension reach in every reader of a behaviour, not in one.
- **Rollback.** A reader revert is a code revert. The pack attributes are additive, and old code ignores them.
