## Context

The rule in `AGENTS.md` is that every category set lives in a vocabulary registry. Code branches only on closed attribute values that a registry schema declares and code implements. Relation types follow the rule in part: they are registry entries, but their `family` attribute is a free string, and code branches on particular free values. Kinds and verdicts do not follow the rule at all.

This change applies the rule to the whole epistemic language.

## Decisions

### Three layers: words, behaviours, dimensions

- **A word** is a registry entry: a relation type, a kind, a verdict, a mood, a belief level. It has a key, labels per language, a description and examples. It keeps its own meaning. `refines` and `qualifies` both narrow a claim, but they say different things and stay distinct entries.
- **A behaviour** is something code does. The registry schema declares the closed set, and code implements each behaviour:
  - `replaces`: hide the target as no longer current.
  - `lifts`: may promote a unit relation to a page proposal.
  - `propagates`: a change to the target puts the source at risk.
  - `membership`: the source belongs to the target.
  - `needs_check`: schedule a revisit by `check_by`.
  - `undercuts`: lower trust in the target.
  - `narrows`: limit the target's scope.
  - `orders`: place the source before or after the target in time.
  - `defines`: the source states what the target means.
  Each word names zero or more behaviours. Adding a behaviour is a code change and a schema change, and that is the point: a behaviour is a promise code keeps.
- **A dimension** groups words by the question they answer. Dimensions are registry data. Each relation family, kind and verdict names one dimension. A vault can add a dimension, and the f38 report and retrieval profiles read dimensions from the registry.

**Why this split:** the owner asked whether the language can be fully dynamic. It can be dynamic in everything except what code does with a meaning. Making behaviours data too would mean code that runs arbitrary rules from a registry, and that turns a vocabulary into a programming language. A small closed behaviour set keeps the code honest, and every word above it stays free.

### Family stays, and gains behaviours and a dimension

A relation family stays the grouping of near-synonyms (`causes` and `caused_by` are both causality). A family entry names its behaviours and its dimension. Today's hard-coded family sets become behaviour declarations in the shipped pack:

- `_LIFT_RELATION_FAMILIES` becomes `lifts` on answer, resolution, question, support, contradiction, refinement, evidence and duplication.
- `MEMBERSHIP_FAMILY` becomes `membership` on membership.

The shipped pack reproduces today's behaviour exactly. A test pins that before and after.

### Kinds and verdicts get one source

Built-in kinds and verdicts move from code into the shipped pack. Every code list reads it:

- `sensing.py` `UNITS_V1`;
- `semantic_blocks.py` `BLOCK_TYPES`;
- `activation.py`;
- `context_pack.py`;
- `collection_candidate.py`;
- `semantic_units.py` `EPISTEMIC_OUTCOMES`.

Kinds carry behaviours:

- hypothesis, prediction and assumption carry `needs_check`;
- decision and constraint carry `narrows`;
- a new definition kind carries `defines`.

Verdicts carry behaviours:

- refuted carries `undercuts`;
- qualified carries `narrows`;
- confirmed and inconclusive carry none;
- abandoned withdraws the claim from active reasoning but keeps it readable.

A refuted claim still keeps active standing, as the commitments require. `undercuts` lowers trust when a reader weighs it. It does not hide the claim.

### Degree of belief, sourced (pending the owner's confirmation)

The recommended shape:

- Belief levels are registry words that any claim may carry: speculative, plausible, likely and near-certain by default.
- A numeric probability is governed unit metadata (`belief: 0.7`). It is valid only beside a `holder` (whose belief) and a `basis` (the evidence or the method).
- Predictions with a probability and a `check_by` can be scored for calibration when their verdict arrives.
- The commitment "a judgment MUST NOT be recorded as a number" becomes "a judgment MUST NOT be recorded as a number without a holder and a basis".

The alternatives were levels only, which keeps the old rule but gives up calibration, and free numbers on any claim, which is the most exposed to unsupported precision. The owner chooses.

### Mood, holder, order, definitions, quantities, procedures

- **Mood** is registry data: is, should, must, may by default. It is a unit attribute, so a norm and a fact about the same subject never merge.
- **Holder** is a unit attribute that names an entity. It generalises "attributed accounts versus established facts": a claim with a holder is that holder's view.
- **Order:** `precedes` and `follows` join the core relations with the `orders` behaviour. The existing `timeline_event` kind gains a governed `at` metadata key.
- **Definitions:** a `definition` kind, plus the `defines` and `is_a` relations.
- **Quantities:** the governed unit metadata keys `value`, `unit` and `uncertainty`. Records remain the place for a series.
- **Procedures:** a `procedure` kind. Approved procedures as skills stay in their own change, which reads this kind.

### Multilingual labels

Each registry entry carries `labels: {language: text}`. Keys stay language-neutral. Parsing accepts any label as an alias of its key, so a unit written in Finnish and one written in English resolve to the same meaning.

## Risks

- **A behaviour change hides in a data migration.** The shipped pack must reproduce today's sets exactly. A before-and-after test over a fixture vault compares lift proposals, membership and verdict handling.
- **Agents misuse the new words.** Each slice proves on real models that an agent picks the right word for realistic and hostile inputs. f38's dimension rows measure the whole.
- **Unsourced numbers creep in.** Validation refuses a `belief` number without `holder` and `basis`, with an error that names both.
