## Why

Exomem is meant to be an epistemic knowledge system. It records what is believed and how far to trust it: when a claim held, where it came from, whose it is, what kind of statement it is, what it rests on and what it is about. Part of that language is already data. Relation types and statuses live in registries that a vault can extend. The rest is fixed in code, or missing.

- **Relation behaviour hangs on keys.** Code decides what a relation does from hard-coded sets of family values or relation keys: `_LIFT_RELATION_FAMILIES` and `_RESOLUTION_RELATION_TYPES` in `epistemic_graph.py`, `MEMBERSHIP_FAMILY` in `context_catalogue.py`, `_PREDICTION_RESOLVING_RELATIONS` in `audit.py` and `_PROVENANCE_RELATIONS` in `activation.py`. A reviewed extension inherits its parent's family, so it reaches the family-keyed sets. It misses most of the key-keyed ones.
- **Kind behaviour hangs on keys.** `semantic_blocks.py` holds the 27 built-in kinds. Other code branches on particular kinds: `_ASSERTION_BLOCK_TYPES` in `activation.py`, and `kind ==` tests in `claims.py` and `artifact_role_review.py`. A vault-added kind parses, but code that reasons about kinds never treats it as an assertion or a procedure.
- **Verdicts are one closed tuple** in `semantic_units.py`. A vault cannot add one or translate one.
- **Nothing groups the language by the question an agent asks:** is this current, where did it come from, whose is it, how sure are we.
- **Missing meanings:**
  - degree of belief, and whose belief it is;
  - whose claim a statement is;
  - normative versus descriptive statements (should versus is);
  - the order of events;
  - quantities with a unit and an error;
  - the mode of an inference (deduction, induction, abduction and others).
- **No numeric confidence:** the bootstrap contract forbids it, to stop unsupported precision. That rule also blocks a sourced forecast that could be scored for calibration.

On 2026-11-08 the owner asked for this language to be dynamic and first-class. Each word keeps its own meaning, and shipped defaults can be extended. Labels are multilingual over one meaning model, and degree of belief is first-class.

## What Changes

- **Closed behaviours, open words.** Code decides what a word does only from closed attributes that the registry schema declares and code implements. It never branches on a relation, kind or verdict key.
  - Core relation entries gain `behaviours` from a closed set, beside the existing `presentation_role`. Each behaviour replaces one hard-coded set, and the shipped pack reproduces today's sets exactly. An extension inherits its parent's behaviours and cannot change them.
  - Kind entries gain `traits` from a separate closed set, for example `assertion` for today's `_ASSERTION_BLOCK_TYPES`.
- **Kinds and verdicts become registry data.** The built-in kinds and verdicts move into the shipped pack, and code reads them from there. Verdicts carry labels and no behaviours, because a verdict is state. An experiment page's `outcome:` follows the verdict registry, as it shares the verdict vocabulary today.
- **Epistemic dimensions become registry data.** A dimension groups relation families, unit kinds and governed metadata keys by the question they answer. The pack ships 12: `currency`, `correction`, `provenance`, `attribution`, `kind`, `argument`, `dependency`, `entities`, `identity`, `scope`, `uncertainty` and `order`. Every dimension, shipped or added, gets filtering in retrieval, grouping in the context compiler, and queues in reviews and reports. A dimension may also name display behaviours from a third closed set: shipped `currency` leads with the current view, and `provenance` shows where each claim came from. Display behaviours change order and annotation only. They never hide a unit or change its standing.
- **Degree of belief becomes first-class.** The owner chose this shape on 2026-11-08:
  - A belief is its own unit of kind `belief`. It states the belief in prose, names its holder (`credence_holder`), its value (`credence`) and its basis (`credence_basis`), and points at the claim it assesses through an `assesses` relation.
  - The value is a belief level (speculative, plausible, likely or near-certain by default) or a probability. A probability needs a basis.
  - The commitment changes from "never a number" to "never a number without a holder and a basis".
  - A belief belongs to its holder. Exomem keeps it as written, accepts any basis, never overrides it and keeps several holders' beliefs side by side.
- **New governed metadata keys**, each reserved:
  - `asserted_by`: whose claim a unit is;
  - `modality`: is, should, must or may by default;
  - `occurred_at`: when a timeline event happened;
  - `quantity_value`, `quantity_unit` and `quantity_error`: a measured quantity;
  - `inference_mode`: deduction, induction, abduction, analogy, statistical inference or testimony by default.
  New reserved keys warn for one release on pages that already use them as ordinary rows, and the warning names the repair.
- **Order in time:** `precedes` and `follows` join the core relations as one inverse pair, with opposite order behaviours so that code reads direction without naming a key.
- **Multilingual labels:** every registry entry carries labels per language over one key and one meaning. Parsing accepts a label as an alias of its key.
- **Teaching surfaces:** bootstrap and the skill teach the language from the registry, never from a code list.

## Impact

- **Data:** existing vaults keep working. The shipped defaults reproduce today's relation and kind behaviour exactly, and existing unit text is unchanged. Definitions and procedures already have kinds (`definition`, `procedure`), and `extends` ("extends or specializes") already covers subtypes. This change adds nothing for them. Extensions of core relations gain their parent's behaviour where key-keyed sets missed them; that change is intended.
- **Code:** each hard-coded set becomes a registry read. That pays down the old C4 debt the survey found.
- **Out of scope, named as debt:**
  - `RECOGNIZED_SECTIONS` in `context_pack.py` lists page section names, not kinds.
  - `CORE_CATEGORIES` in `collection_candidate.py` and the category tests in `artifact_role_review.py` branch on category keys. Categories have their own registry.
  - `UNITS_V1` in `sensing.py` stays versioned instrument data, because a frozen instrument must not change when a registry does.
- **Neighbours:**
  - `add-vocabulary-registries` task S5.1 ("key relation behaviour on family") is delivered here as behaviours.
  - The `relates_to` retyping work and the single-store edge table consume the same relation registry. The edge table stores relation keys, and behaviours resolve at read time.
  - The approved-procedures change reads the existing `procedure` kind.
- **Benchmark:** f38 (`add-epistemic-task-bench`) tags each probe with one of the shipped dimension keys and reports per dimension. f38 does not probe `order`.
- **Proof:** each slice proves on real models that agents use the new meaning correctly. The f38 per-dimension rows describe the whole.
