## Why

Exomem is meant to be an epistemic knowledge system. It records what is believed and how far to trust it: when a claim held, where it came from, whose it is, what kind of statement it is, what it rests on and what it is about. Part of that language is already data. Relation types and statuses live in registries that a vault can extend. The rest is fixed in code, copied in several places, or missing.

- **Unit kinds** (claim, finding, decision, assumption, hypothesis and others) are stated in at least five code lists: `sensing.py`, `semantic_blocks.py`, `activation.py`, `context_pack.py` and `collection_candidate.py`. A reviewed registry extension can add a heading, but code that reasons about kinds never sees it.
- **Verdicts** (abandoned, confirmed, inconclusive, qualified, refuted) are one closed tuple in `semantic_units.py`. A vault cannot add one or translate one.
- **Relation families** are free strings. Code branches on particular values in hard-coded sets, for example `_LIFT_RELATION_FAMILIES` in `epistemic_graph.py` and `MEMBERSHIP_FAMILY` in `context_catalogue.py`. A new family gets no behaviour, and nothing groups families into the questions an agent asks.
- **Missing dimensions:**
  - degree of belief;
  - normative versus descriptive statements (should versus is);
  - whose viewpoint a claim is;
  - the order of events;
  - definitions;
  - quantities with units and uncertainty;
  - how-to knowledge.
- **No numeric confidence:** the bootstrap contract forbids it, to stop unsupported precision. That rule also blocks a sourced forecast that could be scored for calibration.

On 2026-11-08 the owner asked for this language to be dynamic and first-class. Each word keeps its own meaning, and shipped defaults can be extended. Labels are multilingual over one meaning model, and degree of belief is first-class.

## What Changes

- **Behaviours are the only fixed set.** A small closed set of behaviours names what code does with a meaning:
  - hide as replaced;
  - lift to a page proposal;
  - propagate along a dependency;
  - treat as membership;
  - schedule a check;
  - lower trust;
  - narrow scope;
  - order in time.
  A registry schema declares the set and code implements it. Code branches only on behaviours, never on a kind, verdict, family or relation key.
- **Kinds, verdicts and families become registry data.** Each kind, verdict and relation family is a registry entry with shipped defaults and vault additions, and each names the behaviours it carries. A new entry works the moment it names behaviours code already implements. Every code copy of the kind and verdict lists reads the registry.
- **Dimensions become registry data.** A dimension groups relation families, kinds and verdicts by the question they answer:
  - is this still current;
  - where did it come from;
  - whose is it;
  - what does it rest on;
  - what is it about;
  - how sure are we;
  - is it a fact or a norm;
  - when did it happen.
  Every family names one dimension, and the registry check refuses a family without one. The name is "dimension", because "family" already has four other meanings in the code.
- **Degree of belief becomes first-class.** The owner chose this shape on 2026-11-08:
  - belief levels (speculative, plausible, likely, near-certain) are registry data that any claim may carry;
  - a numeric probability is allowed only with a holder and a basis, mainly on predictions and hypotheses, so it can be scored for calibration;
  - the commitment changes from "never a number" to "never an unsourced number".
- **New first-class meanings,** each as registry defaults:
  - a statement's mood (is, should, must, may);
  - the holder of a claim (whose viewpoint, as an entity);
  - event order (`precedes` and `follows`, with the order-in-time behaviour);
  - definitions (`defines` and `is_a`);
  - quantities (value, unit, uncertainty as governed unit metadata);
  - procedures (a procedure kind, aligned with the approved-procedures change).
- **Multilingual labels:** every registry entry carries labels per language over one key and one meaning.
- **Teaching surfaces:** bootstrap and the skill teach the language from the registry, never from a code list.

## Impact

- **Data:** existing vaults keep working. The shipped defaults reproduce today's kinds, verdicts and family behaviour exactly, and existing unit text is unchanged.
- **Code:** each hard-coded list becomes a registry read. That pays down the old C4 debt that the survey found.
- **Neighbours:**
  - `add-sensed-epistemic-model` reads its `units-v1` kinds from the kinds registry.
  - `add-vocabulary-registries` task S5.1 ("key relation behaviour on family") is delivered here as behaviours.
  - The `relates_to` retyping work consumes the same relation registry.
  - The single-store edge table stores relation keys, and behaviours are resolved at read time.
- **Benchmark:** f38 (`add-epistemic-task-bench`) tags each probe with a dimension key from this registry and reports per dimension.
- **Proof:** each slice proves on real models that agents use the new meaning correctly. The f38 per-dimension rows measure the whole.
