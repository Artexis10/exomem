## Context

The rule in `AGENTS.md` is that every category set lives in a vocabulary registry. Code branches only on closed attribute values that a registry schema declares and code implements (`family`, `role`, status `class`). It never branches on an entry key.

Relation types follow the rule in part. They are registry entries, and a namespaced extension must name one core parent and inherit its family (`family_mismatch`). But code branches on particular family values and relation keys. Kinds and verdicts do not follow the rule at all.

This change applies the rule to the whole epistemic language.

## Decisions

### Three layers: words, behaviours, dimensions

- **A word** is a registry entry: a relation type, a kind, a verdict, a mood, a belief level, an inference mode. It has a key, labels per language, a description and examples. It keeps its own meaning. `refines` and `contradicts` both bear on a claim, but they say different things and stay distinct entries.
- **A behaviour** is something code does. The registry schema declares each closed set, and code implements each member. Adding one is a code change and a schema change, and that is the point: a behaviour is a promise code keeps. There are three sets, because each has its own target: relation behaviours, kind traits and dimension display behaviours.
- **An epistemic dimension** groups words by the question they answer. Dimensions are registry data, and a vault can add one.

**Why this split:** the owner asked whether the language can be fully dynamic. It can be dynamic in everything except what code does with a meaning. Making behaviours data too would mean code that runs arbitrary rules from a registry, and that turns a vocabulary into a programming language. Small closed sets keep the code honest, and every word above them stays free.

### Relation behaviours live on core relation entries

A core relation entry gains a `behaviours` list, beside its existing `presentation_role`. An extension inherits its parent's behaviours and cannot set its own, just as it inherits the parent's family. Vaults do not create families.

Behaviours sit on relations, not families, because today's sets are keyed both ways. Each behaviour replaces one set:

| Behaviour | What code does | Replaces | Core relations |
|---|---|---|---|
| `lifts` | A unit relation may become a page proposal. | `_LIFT_RELATION_FAMILIES` | answers, resolves, raises_question, supports, contradicts, refines, evidenced_by, duplicates |
| `membership` | The source belongs to the target. | `MEMBERSHIP_FAMILY` | member_of |
| `bears_on` | An edge into a prediction or question counts toward settling it. | `_PREDICTION_RESOLVING_RELATIONS`, `_QUESTION_ANSWERING_RELATIONS` | supports, contradicts, resolves, evidenced_by |
| `settles` | The graph reads the edge as resolving a question. | `_RESOLUTION_RELATION_TYPES` | answers, resolves |
| `sources` | Activation counts the edge as provenance. | `_PROVENANCE_RELATIONS` | derived_from, evidenced_by, cites |
| `replaces` | The target is no longer the current view. | `kind == "supersedes"` tests | supersedes |
| `orders` | The source comes before the target in time. | new | precedes, follows |

`presentation_role` stays renderer metadata: it tells a viewer how to draw an edge. Behaviours decide what code does with the edge. `supersedes` carries both, for those two different reasons.

Slice 2 starts with an inventory of every branch on a relation key or family value in `src/exomem`. Known so far beyond the table: further `supersedes` tests in `relation_census.py` and `sensed_model.py`, `derived_from` in `dreamer_families.py`, and `links_to` in `governance/projection_runtime.py`. Each branch maps to a behaviour above, or to a new behaviour added with its code, or is named as debt. A before-and-after test over a fixture vault compares lift proposals, membership answers, prediction and question audits, resolution, activation counts and role review for core relations. Extensions change on purpose: an extension of `supports` or `derived_from` now gains its parent's behaviour in the audit and activation, which today match on the extension key and miss it. The fixture holds one such extension with its expected new result.

### Kind traits are a separate closed set

Kinds need their own set, because a relation behaviour has a source and a target and a kind has neither. A kind entry gains a `traits` list:

| Trait | What code does | Replaces | Shipped kinds |
|---|---|---|---|
| `assertion` | Activation counts the unit as an assertion. | `_ASSERTION_BLOCK_TYPES` | claim, finding, inference, hypothesis, result |
| `page_claim` | The claims reader takes the first such unit as the page's claim. | `kind == "claim"` in `claims.py` | claim |
| `procedure` | Role review reads the unit as a procedure. | `kind == "procedure"` in `artifact_role_review.py` | procedure |
| `outcome` | Role review reads the unit as an outcome. | `kind == "result"` in `artifact_role_review.py` | result |

The slice-3 inventory adds any further kind branch the same way.

A trait never schedules a check. `check_by` already does that on any unit, so a trait for it would duplicate the governed key.

The 27 built-in kinds in `semantic_blocks.py` move into the shipped pack. `definition` and `procedure` are among them already.

### Verdicts are state

Verdicts become a registry with shipped defaults (abandoned, confirmed, inconclusive, qualified, refuted), labels and descriptions. A vault can add a verdict or translate one. An experiment page's `outcome:` shares the verdict vocabulary today through one constant, so it follows the registry too: a vault-added verdict is a valid outcome. Verdicts carry no behaviours. A verdict changes no lifecycle, standing or rank, as today, and a refuted claim keeps active standing.

### Epistemic dimensions

A dimension entry has a key, labels, a description, its members and its display behaviours. Members are relation families, unit kinds and governed metadata keys. One member may sit in several dimensions, because a word can answer more than one question. A unit belongs to a dimension when its kind is a member, when it carries a member key, or when it is an endpoint of an edge whose family is a member.

**Every dimension gets the generic operations:** retrieval filters by it, the context compiler groups by it, and reviews and reports queue by it. No code names a particular dimension.

**A dimension may also name display behaviours** from a closed set:

- `lead_with_current`: put current members before replaced ones, and mark the replaced ones;
- `show_origin`: show beside each member where it came from and whose it is;
- `flag_open`: mark members that have no closing verdict or answer as open;
- `order_in_time`: present members in time order, read from `orders` edges and `occurred_at`. It is the only rule that orders events.

A display behaviour applies only within its own dimension's group. When a request groups by several dimensions, a unit that belongs to more than one appears in full once and as a ref in the other groups, so orders cannot conflict.

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

Every new key name is chosen to avoid ordinary rows: `value`, `at`, `holder`, `basis` and `mood` are common words in existing notes, and `credence`, `occurred_at` and `modality` are not.
- `inference_mode` takes an inference-mode registry key on a unit with the `assertion` trait.

Each new key is reserved, as `verdict` and `check_by` are. A page that already uses one as an ordinary row gets a warning, not an error, for one release. The warning names the repair: rename the row, or give it a valid value.

### Multilingual labels

Each registry entry carries `labels: {language: text}`. Keys stay language-neutral. Parsing accepts a label as an alias of its key, so `## Väite` and `## Claim` are one kind. The registry check refuses a label that collides with another entry's key or label.

## Risks

- **A behaviour change hides in a data migration.** The shipped pack must reproduce today's sets exactly. The before-and-after fixture test guards each cutover.
- **Agents misuse the new words.** Each slice proves on real models that an agent picks the right word for realistic and hostile inputs.
- **Unsourced numbers creep in.** Validation refuses a probability without a holder and a basis, with an error that names the missing key.
- **Display annotations add noise.** `flag_open` marks every risk and assumption without a verdict. Slice 5 checks on real models that the annotations help, and f38's P arm shows their effect on outcomes.
- **Belief units add authoring weight.** A separate unit costs more than a row on the claim. In return, each belief keeps its holder, and several holders can disagree on record.
