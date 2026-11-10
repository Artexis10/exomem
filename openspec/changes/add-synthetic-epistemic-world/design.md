## Context

The repository already holds most of the machinery that a generated benchmark needs.

- **membench** (`benchmarks/membench/`) has a strict record schema, a bitemporal oracle that derives every expected answer, a privacy-safe wordbank, native renderers with per-fact parity reports, and a runner. Its unit of authoring is the scenario template: a Python function that builds its own entities and writes each status span by hand.
- **The epistemic task suite** (f38, `benchmarks/membench/outcome/epistemic/`) plays an actor through a project of work sessions and scores probes with a blind judge. Its runner, case check, development gates and statistics exist. Its three projects are hand-written, in evaluator custody.
- **The Epistemic State Bench** (`benchmarks/epistemic/`) has a frozen family and assertion registry, an amendment protocol and control twins.
- **The shipped vocabulary pack** (`src/exomem/vocabulary/packs/core/`) is the model for the catalogue: open words in YAML, and closed attribute sets that code implements.

Three earlier lessons bind this design.

- The 2026-08 audit of membench's head-to-head run found the comparison unsafe in both directions (`docs/memory-proof-benchmark.md`). Whoever writes a harness shapes its outcome, with or without a competitor in the frame, so the writer cannot vouch for its fairness.
- The 2026-08-09 rescope withdrew every cross-product comparison authored in this repository. That rule stands.
- A generated corpus predicts a cost only on the axes where its data matches real data. Elsewhere it can rank two changes and cannot predict an absolute number.

## Goals / Non-Goals

**Goals:**

- One catalogue that can hold hundreds of epistemic phenomena, with a structure that does not change as it grows.
- Every key derived in code from a generated world, for every phenomenon the catalogue marks as computable.
- A world whose documents carry their statements implicitly and messily, with the truth still known.
- A score that shows how far each arm sits from a floor and a ceiling, per family.
- Slices that are each useful alone. The first one feeds the f38 development round and the display-marks check.

**Non-Goals:**

- A leaderboard, a single score or a published comparison of products.
- A replacement for the hand-written f38 projects. They stay the independent anchor.
- A migration of membench's 24 templates. They keep working as they are.
- The 50,000-page latency world and the sensors' training export. This design fixes their seams and builds neither first.

## Terms

| Term | Meaning |
|---|---|
| World | One generated universe from one seed: entities, events and the statements made about them. |
| Catalogue | The registry of phenomena, families and their closed attributes. |
| Phenomenon | One catalogue entry, such as `currency.reversal`. |
| Thread | One planted instance of a phenomenon in a world. |
| Statement | One thing that a holder said, wrote, measured or decided, with its content, time, kind and basis. |
| Artefact | One rendered document that carries statements: a message, a note, a file, a table or a log. |
| Warranted state | What the statements up to a date support, under the precedence that the world's own records state. |
| Oracle | The code that derives the warranted state and every key. |
| Probe | A task with a key. |
| Projection | An output of a world for one instrument or one product. |
| Round | One run of a set of arms over one world. |
| Dial | A difficulty setting that a round declares before it runs. |

## Decisions

### 1. Truth comes first, in three layers

The generator builds a world in three layers, and text comes after all three.

1. **The world.** Entities, their attributes over time, the events that change them, the rules and causes that connect them. This layer is complete and consistent. No actor ever sees it.
2. **The evidence.** The statements that holders made: who said what, when, of what kind, how hedged, on what basis. A statement may be late, wrong, partial, biased or false, and the generator records how each one differs from the world.
3. **The warranted state.** What the evidence up to a date supports. The oracle derives it from the evidence alone.

**Keys come from the warranted state, not from the world.** Suppose the world changed and no statement recorded it. Then the warranted answer is the old value with its date. If the evidence supports no answer, the key is an abstention, although the world holds a value.

- **Rejected: keys from the hidden world.** That punishes a correct abstention and rewards a lucky guess.
- **Rejected: text first, keys extracted afterwards.** An audit that the Veracium paper cites found 6.4% of one benchmark's answer keys corrupt. STALE's scenarios are model-written and judge-validated, so their truth is asserted, not constructed.
- **What would change this:** the gold-evidence arm disagreeing with warranted-state keys on more than one probe in ten in a family. Then that family's precedence rules are wrong, or its entries belong on the rubric track.

**The precedence that the oracle applies is evidence too.** Which record governs a fact is stated in the world's own documents, and it can change on a date. The oracle holds no hidden rule about whom to believe. This keeps the key reachable by any careful reader, whatever memory system it uses.

### 2. The catalogue is open, and the truth mechanisms are closed

This is the shipped vocabulary's rule, applied to the benchmark: open words, closed behaviours.

- **Open:** phenomena, families, behaviours, domain packs, surface forms and bridges. Each is a YAML entry. Adding one is a data change.
- **Closed:** truth mechanisms, key classes, probe forms, implicitness levels, stages, scorers and dials (`catalogue/attributes.yaml`). Code implements each member. Adding one is a code change and a schema change.

Code never branches on a phenomenon id or a family key. A phenomenon is a composition of closed parts, so the 516 entries need 13 mechanisms and 13 probe forms.

- **Rejected: one template function per phenomenon.** That is membench today. It puts vocabulary in code and does not reach hundreds of entries.
- **Rejected: a rule language in the catalogue.** Data that defines its own truth rules turns a vocabulary into a programming language. The epistemic-language design rejected the same move for the same reason.
- **What would change this:** more than a quarter of the slice 2 entries needing a mechanism that the set lacks. Then the set is wrong, and the catalogue's structure does not hold.

### 3. One coherent world, with planted threads that do not interfere

A world has a backbone and threads.

- **The backbone** is ordinary life in one domain pack: people, teams, systems, suppliers and routines. It produces the stable facts, the background documents and the distractors. Its shape comes from a profile: how many entities, how busy each one is, how long documents are, how often a fact is restated.
- **A thread** plants one phenomenon on backbone entities. A recipe turns the catalogue entry into world operations: set a value, make a statement, retract one, rename an entity, add a dependency. A recipe fires only when the world holds what it needs, as RECON's production rules do.
- **The planner reserves** every attribute that a thread's key reads. No other thread may change a reserved attribute. Each probe therefore depends on one planted change, which f38 requires.

Compound threads, which combine two phenomena on purpose, come in slice 4 and declare both.

- **Rejected: scenario islands.** No island shares an entity with another, so nothing interacts and no distractor is natural.
- **Rejected: a free simulation of agents.** Its events are uncontrolled, so no phenomenon is guaranteed to occur and no key is guaranteed to be unique.
- **What would change this:** reservation leaving too few free attributes to plant 30 threads in a 12-session world. The first answer is more entities per pack, not another design.

### 4. Extend membench, and emit its records

The generator is a subpackage, `benchmarks/membench/world/`. It reuses the wordbank, the id scheme and the outcome suites. Its membench projection emits `EntityRecord`, `SourceRecord`, `ClaimRecord`, `QueryRecord` and `ExpectedRecord`, so the existing runner, adapters and gates read a world without change.

The gap that needs new code:

| membench today | The world adds |
|---|---|
| Templates write each status span by hand. | The oracle derives each span from statements, and membench's span lint then checks it. |
| Each template owns its entities. | One backbone, shared by all threads. |
| A source asserts, disputes or retracts. | A statement also has a holder, a kind, a hedge, a basis and a scope. |
| Text states each fact in plain template lines. | Seven implicitness levels. |
| Output is sources and queries. | Work sessions with probes, a vault seed, and later a large world and a training export. |
| Families and statuses are Python constants. | The catalogue is YAML. |

- **Rejected: a new top-level package.** It would copy the ids, the wordbank, the schema and the runner.
- **Rejected: depend on Veracium's generator.** It is MIT-licensed and builds facts before text, which is the right shape. It emits a personal chat and email script with 15 question types. It has no holders' beliefs, kinds, arguments, dependencies or scopes, and no work sessions. It needs a newer Python, and a memory product's team maintains it.
- **Rejected: adopt RECON's generator.** The paper states no code licence, and this design could not open its anonymous repository. Its cases are single long case files.
- **What would change this:** a licensed RECON release that emits session streams, or a Veracium release that covers most of the mechanisms. Either would replace the planner, not the catalogue.

The world oracle, like membench's, imports nothing from `exomem`. A test asserts it.

### 5. Implicitness is a rendering setting, and bridges are data

Real difficulty lives in statements that nobody marks. The design plants them without losing the truth because a statement's content is fixed in the evidence layer before any text exists.

| Level | The text | Example for a move to another town |
|---|---|---|
| `stated` | States the content and its status. | A note says that the office moved, and names the new town in place of the old. |
| `paraphrased` | States it in words that the probe does not use. | A note says that the team relocated. |
| `unmarked` | Gives the new content with no word of change. | A letterhead shows the new town. |
| `implied` | States an observation that entails it. | A message arranges utilities at an address in the new town. |
| `propagated` | Changes another attribute that it depends on. | The lease on the old premises ended. |
| `embedded` | Carries it in a side remark, a file value or metadata. | A delivery form holds the new postcode. |
| `silent` | Carries it by an absence. | The old town's weekly report stops. |

- **A surface form** is one text pattern for one statement kind, level and artefact kind. Surface forms are YAML in the domain pack.
- **A bridge** links an attribute to the observations that entail it. Each bridge declares its strength: `entails` or `suggests`. Only an `entails` bridge yields a computed key. A `suggests` bridge yields a rubric-track probe.
- **The ceiling arm audits every bridge.** A bridge that the gold-evidence arm fails to read on the development split is unfair, and the pack drops it.
- **Probes use their own surface forms.** No probe shares wording with the artefact that answers it. An audit of membench found the opposite defect: a renderer that phrased facts in the question's vocabulary.

The level and the bridge of every artefact go into the world's manifest, so a report can split results by level.

**Model-written prose is optional and gated.** Templates are the default renderer and cost nothing. A model may rewrite an artefact for natural, messy prose. Then a check must pass in both directions:

1. **Anchors.** Every name, value and date of the planted statements is present. No trap value appears in an artefact that must not hold it. No explicit form appears in an artefact rendered below `stated`.
2. **Round trip.** A model of another family reads the rewritten artefact blind and lists its statements through a structured-output schema. Every planted statement must come back, and nothing may come back that the manifest lacks.
3. **Fallback.** A note that fails twice keeps its template rendering, and the manifest records that.

The check itself is tested by mutation: the suite drops and adds known facts and counts how many the check catches. The key never reads rendered text.

- **Rejected: templates only, for ever.** RECON's authors note that a mechanically written file can be solved by pattern matching.
- **Rejected: a presence-only check.** Veracium's verifier finds a missing fact and not an added one, and its paper names that as a limit.
- **What would change this:** a round trip that cannot catch nine planted mutations in ten at a bearable cost. Then model prose stays off, and the surface banks grow instead.

### 6. Every family has a floor and a ceiling

A score alone says nothing. Each round plays reference arms beside the memory arms.

| Arm | What the actor has | Role |
|---|---|---|
| `N` | No memory and no history. | Floor. |
| `G` | No memory. The probe's proof trace: the few artefacts that carry its answer. | Ceiling. |
| `H` | No memory. The full text of every earlier session. | Description, on development worlds. This is f38's solvable gate. |
| `F` | A plain memory folder, with the product's learning instruction. | Baseline store. |
| `P` | Exomem as shipped. | Primary. |
| `E` | Exomem over MCP alone. | Ablation. |

- The oracle writes each probe's **proof trace**, as RECON does. `G` and `N` need probe calls only, so the two reference arms are cheap.
- A report gives each arm's **position** in a family: (arm − `N`) / (`G` − `N`), with the paired differences and their intervals beside it.
- When the interval of `G` − `N` reaches below 0.2, the family reads **no room**: the probes do not need memory, or nobody can solve them. That is a finding about the probes.
- When `P` sits within 0.05 of `G` in a family on two rounds in a row, the family reads **saturated**. It becomes a regression check, and the next round raises a dial for it: implicitness, distance, distractors, restatements, composition, history or margin.
- A dial is declared before a round and never changed after a held-out result.
- Control twins balance the catalogue. Fifteen entries plant a case that looks like its partner and needs the opposite behaviour, such as two statements that agree. An arm that flags everything fails them.

There is no aggregate score. A family that a round did not sample reads `not measured`, never 0.

- **Rejected: a table of accuracies.** A home benchmark at 100% tells nobody anything, and one at 40% does not say whether the ceiling is 45 or 95.
- **What would change this:** nothing in view. The thresholds 0.2 and 0.05 are first values, and the first two development rounds may move them before any held-out world exists.

### 7. Predictions are filed before a run

`catalogue/projections/exomem.yaml` states, per family, whether Exomem models it and what this design expects: pass, partial or fail. A run's manifest pins that file's digest, and the report prints each prediction beside its result.

The first register expects no family to pass. It expects 13 of 35 to fail: persistence, source reliability, calibration, propagation, scope, quantity, statistics, meaning, inference, causation, belief states, deception and machine sources. It expects the implied and propagated levels to fail in every family. These are readings of the shipped pack and the active specs. No run backs them.

A wrong prediction is a finding in either direction. A family that passes against its prediction needs its probes read before anyone believes the pass.

### 8. Splits are separate worlds

| Split | Seed | Use |
|---|---|---|
| `train` | Public. | Labelled spans for distilling the model-based sensors. Never scored. |
| `dev` | Public. | Iterating the generator, the harness and the product. |
| `heldout` | In evaluator custody. | One decision. Then the world is retired to `dev`. |

- A world's seed derives from the master seed, the split and the world's index. The split is part of every identifier.
- The three splits share no name and no surface form. The wordbank and each surface bank assign every entry to one split by a stable hash of its id.
- A loader refuses to mix splits. The training export refuses a world that is not `train`. A sensor's training manifest lists the digests of its inputs, and a round refuses a sensor trained on a `dev` or `heldout` digest.
- The repository holds a commitment to each held-out world: the digest of its seed and of its manifest. The seed itself never enters the repository, a prompt or a fixture.
- A held-out world is generated only from a tagged generator. No probe of it is repaired or dropped after its run. The development split's repair rate is the stated estimate of its flawed probes.

- **Rejected: one world, split by probe.** Probes of one world share entities and facts, so a held-out probe leaks through a development one.
- **Rejected: a fixed held-out bank.** Every reuse spends it. A generator makes a fresh one for each decision.

### 9. Generated worlds decide nothing until an independent lane has attacked them

The generator, the catalogue and the Exomem projection come from Exomem's own lanes. That is the bias the fairness finding describes.

1. Slice 1 feeds **development rounds only**. The hand-written f38 projects stay the cases that an f38 decision reads.
2. Before generated worlds decide anything, a lane that wrote none of this reviews the generator, the catalogue, the surface banks and the projections. Its brief is to find every way they disadvantage the folder arm or another product.
3. A **concordance check** plays the hand-written development project and a generated one in the same window. If the two disagree on which arm leads in a dimension they share, the generated cases are suspect until the cause is found.
4. A preregistration amendment then registers generated worlds as their own family, with their decision rules. The existing protocol withholds the family until the owner acknowledges the receipt.

- **Rejected: replace the hand-written held-out projects now.** They are the only cases that the generator's authors did not shape.

### 10. A judge decides free text, and code scores structure

f38's rule stands: a blind judge decides whether a free-text answer is correct, from the task, the key and the answer. The judge is the strongest available model, from another family than the actor where one is available, and a second-family sample re-judges for description.

Where a probe asks for a structured field, code scores it: an exact value, a set, an order by Kendall's tau, or a probability by the Brier score. A judge is a poor instrument for an order or a probability.

Keys carry the value, its accepted forms, the trap values, the behaviours and the proof trace. Accepted forms come from typed formatters and the identity map, never from a system's output.

### 11. The world is product-neutral, and the product enters through a projection

- The evidence layer uses the W3C PROV names: entity, activity, agent, derivation and attribution. It uses no Exomem kind, relation or dimension.
- The Exomem projection is the only file that names a product. It maps a family to the dimension key that f38 reports.
- A fairness gate runs on the adapter side: the neutral artefacts, parsed by each product's own grammar, must yield no typed unit and no typed relation. The world's text then gives no product a head start.
- Every projection writes a parity report, as membench's native renderers do: each fact is represented, degraded or unsupported.
- Arm `F` receives the same sessions, files and learning instruction as `P`.
- A neutral export, built in slice 2, lets another product's own team run a world. This repository authors no competitor integration and publishes no cross-product table.
- 101 of the 516 entries name an outside work as lineage. The other 415 come from this project. A report states the share of its probes that come from each group, so a reader can discount the home-defined ones.

### 12. Cost is designed in

| Layer | What runs | Model usage |
|---|---|---|
| 0 | Generation, the oracle's invariants, projections and leak checks, in CI. | None. |
| 1 | A product over the membench projection, with deterministic gates. | None. |
| 2a | An outcome round with the pinned low-cost actor of the utility instrument. | Metered, capped at $2 a round. |
| 2b | An outcome round with the subscription actor, for a decision. | Subscription turns. |

- A round is small: one world, a declared set of families, and four to six arms. The reference arms add probe calls only.
- A development round uses a fresh world, so a repair is checked on the cases that exposed it and judged on new ones.
- Every paid run states its cost and the question it answers before it starts. The f38 estimate of $20 to $25 for a full development round is unverified, and this design repeats it only as that.
- Floor and ceiling results are not reused across time windows. A provider's behaviour moved within hours on 2026-11-08, so all arms of a decision run in lockstep.

## The catalogue

`catalogue/` holds the first catalogue as data.

| File | Holds |
|---|---|
| `attributes.yaml` | The closed sets: 13 truth mechanisms plus `judged` and `none`, 3 key classes, 13 probe forms, 7 implicitness levels, 5 stages, 6 scorers, 7 dials. |
| `behaviours.yaml` | 21 behaviours that a judge rates, each wanted or unwanted. The first six are f38's. |
| `families.yaml` | 9 realms and 36 families, each with its question and its default attributes. |
| `phenomena/<realm>.yaml` | 516 entries. An entry states what differs from its family's defaults. |
| `lineage.yaml` | 34 sources, each with what this benchmark adopts and leaves, and how it was checked. |
| `projections/exomem.yaml` | The Exomem dimension and the filed prediction per family. |

An entry reads:

```yaml
- {id: propagation.independent-support, stage: s2, says: "A conclusion loses one support and survives on another.", from: [recon, agm]}
```

It inherits the family's mechanism (`warrant_graph`), key class, probe forms (`impact`, `act`) and behaviours.

Size by family, counted on 2026-11-10 with `grep -ho 'id: [a-z]*\.' catalogue/phenomena/*.yaml | sort | uniq -c`:

| Realm | Families and entries |
|---|---|
| Time and change (48) | currency 18, order 18, persistence 12 |
| Sources and testimony (64) | provenance 18, attribution 16, reliability 16, testimony 14 |
| The status of a statement (60) | kind 19, modality 13, uncertainty 16, calibration 12 |
| Conflict and revision (61) | correction 16, conflict 15, argument 15, propagation 15 |
| Reference and scope (91) | identity 16, entities 16, scope 17, quantity 18, statistics 12, meaning 12 |
| Inference (46) | inference 18, causation 14, absence 14 |
| Minds and society (63) | belief 14, consensus 11, commitment 15, deception 13, disclosure 10 |
| The store itself (58) | hygiene 14, self 12, procedure 12, media 12, language 8 |
| Beyond human sources (25) | machine 18, limits 7 |

By stage: 60 in slice 1, 216 in slice 2, 192 in slice 3, 41 in slice 4, and 7 declared out of scope. By key class: 496 computed, 13 on the rubric track, 7 out of scope.

The key classes are a first assignment. Building an entry confirms its class or moves it to the rubric track, and the ceiling arm audits each one.

**How the catalogue reaches thousands.** A case is a phenomenon crossed with the round's settings. The settings are the implicitness level, the probe form, the domain pack, the artefact kind, the distance and the distractor load. The catalogue is the open axis. The other axes multiply it without new authoring.

## Generator architecture

```
catalogue + domain pack + profile + seed
        │
        ▼
  backbone ──► planner ──► world ──► evidence ──► oracle ──► warranted state, keys, proof traces
                 (threads,            (statements)                 │
                  reservations)                                    ▼
                                             renderers ──► artefacts (level, bridge, surface form)
                                                                   │
                                                                   ▼
                                    projections: f38 project · vault seed · membench corpus · …
```

1. **Load.** The catalogue loader validates every closed value and every reference. An unknown value stops the load with the entry's id.
2. **Backbone.** The domain pack and the profile build entities, routines and stable facts. Names come from the wordbank's partition for the split.
3. **Plan.** The planner draws threads for the round's families, checks each recipe's needs, reserves attributes and places plants and probes on the session timeline. A probe never shares a session with an artefact that holds its answer.
4. **World and evidence.** Recipes run as world operations and statements. A validator checks the world's invariants: times in order, one holder per role at a time unless stated, sums that add up.
5. **Oracle.** Each mechanism derives its part of the warranted state at each probe's date. The oracle writes the key, the trap values and the proof trace, and checks that each key is unique.
6. **Render.** Each statement gets a surface form at the round's implicitness level. The anchor check runs on every artefact.
7. **Project.** Each projection writes its output and its parity report.
8. **Manifest.** The manifest pins the generator version, the catalogue digest, the pack digests, the seed commitment and every artefact hash. Generated output is never committed.

A world for one f38 project is small: 12 sessions, about 30 threads and a few hundred artefacts. The same pipeline at a larger profile gives the latency world.

**Domain packs.** Slice 1 ships three, matching f38's settings: a small software and operations team, a research and data project, and a small business's operations. A pack is YAML: entity types, attribute schemas with value generators, event types, artefact kinds, work-task templates, surface forms and bridges. Later packs add settings that other families need, such as a field station with instruments for machine sources.

**A worked thread.** `currency.reversal` in the operations pack, at level `unmarked`:

1. The planner picks the attribute `approvals(staging deploys)` and reserves it.
2. The world sets it to 1 in week 2 and to 2 in week 6. The team lead decides both.
3. The evidence holds two statements by the lead, of kind decision. A third statement in week 8, by a contractor, repeats the old value from memory.
4. The world's handbook says that the lead's decisions govern deploy rules. `state_fold` gives 2 as current from week 6, with 1 as the trap.
5. The renderer writes the week 6 statement as a changed line in a checklist file, with no word of change.
6. Session 10 asks the actor to prepare a staging deploy. Its files state neither number. The key is 2, the trap is 1, and the proof trace is the checklist file and the handbook page.

## How each mechanism knows the truth

| Mechanism | The generator records | The oracle derives |
|---|---|---|
| `state_fold` | Dated statements per attribute, and the precedence the world states. | Current value, history, value at a date, trap values. |
| `interval_order` | Event times with their recorded precision. | Four-valued order, overlap, duration, computed dates. |
| `warrant_graph` | Support, derivation and defeat edges, induced by the recipes that made them. | What stands, what falls after a withdrawal, what survives on other support, the count of independent origins. |
| `holder_ledger` | What each holder said, saw and was told, and when. | Whose claim it is, who knows it, what one holder thinks another believes. |
| `kind_tag` | The kind, modality and hedge of each statement. | The status that an answer must keep. |
| `identity_map` | Names, aliases, merges, splits and role holders over time. | The referent of a name at a date. |
| `scope_lattice` | The contexts where each statement holds. | Whether a rule applies to the probe's case. |
| `quantity_calc` | Typed values with unit, precision and error. | The result, its unit and its tolerance. |
| `chance_model` | The probability behind each drawn outcome and each source's error rate. | The best forecast, and the score of a stated one. |
| `causal_model` | The structural model that produced the events. | A cause, the effect of an intervention, a counterfactual, by running the model again. |
| `rule_closure` | The rules and facts that the world's documents state. | Every conclusion that follows, with its steps. |
| `policy_view` | Audiences, restrictions and deletion requests. | What the asker may see, and what must be withheld. |
| `corpus_closure` | The complete list of artefacts, authors, dates and copies. | Never recorded, recorded as false, duplicated, orphaned or withheld. |

Three checks guard the oracle, and none uses a model.

- **Mutation.** Removing a thread's planted change must move its key to the stable value. A key that does not move was never tied to the plant.
- **Invariance.** Adding an unrelated statement, or renaming every entity consistently, must leave every key the same up to the renaming. Withdrawing one support must leave standing every conclusion that has another, as AGM's minimal change requires.
- **Uniqueness.** A probe whose key is not unique under its mechanism is refused at generation.

Rubric-track entries have no computed key. The oracle supplies the facts, a predeclared rubric cites them, and a blind judge applies it. Out-of-scope entries generate nothing. They state where the benchmark stops.

## Projections

- **f38 project (slice 1).** `project-<x>.json` and `SPLIT.json` in the suite's own format: sessions with a task and files, and probes with a key. A probe's dimension key comes from the Exomem projection. The existing case check runs unchanged. A family with no Exomem dimension keeps its family key as the probe's label, and its rows are description only.
- **Vault seed (slice 1).** The warranted state at a date, written through Exomem's own tools on a disposable vault, as the planted suite's builder does. The projection lists the display marks that each unit should carry: replaced, open, origin and order. A model-free check reads the marks back and compares them. A second vault holds the same text without the typed structure, so a later model round can ask whether the marks help.
- **membench corpus (slice 2).** Records in membench's schema, for the model-free layer.
- **Large world (slice 4).** The same pipeline under a size profile. The profile names each cost-driving axis: link density, document length, folder spread and duplicate rate. Its values are aggregate shapes only.
- **Training export (slice 4).** Text spans from `train` worlds with their labels from the evidence layer.
- **Neutral export (slice 4).** Sessions and documents as plain JSON, for another product's own harness.

## What comes from outside work

`catalogue/lineage.yaml` holds the full list. The main debts:

| Work | Adopted | Left |
|---|---|---|
| RECON | Production rules, a provenance graph from the generation contract, reach after invalidation, proof traces, a structured-truth ceiling, models for surface text only. | Its generator and its single-file cases. |
| STALE | Implicit conflict by two conditions, its two types, its three probes. | Model-written scenarios. |
| MemConflict | Three conflict types, validity as fitness for the question, distance and distractors as dials. | Dialogue-only histories. |
| MemoryAgentBench | Its four competencies as a coverage check. | Edits to real-world facts. |
| LongMemEval | Five abilities as a coverage floor. | A fixed bank as the development loop. |
| Veracium | Facts before text, volatility classes, an answerability check, a second-family judge. | Its generator, and a presence-only fidelity check. |
| FrontierMemory | Memory judged by success on later work, with holding a fact scored apart from using it. | Its 15 fixed tasks. |
| Truth maintenance, AGM, Pollock, Dung | The warrant graph, the oracle's invariants, two kinds of defeat, which arguments stand. | Full logics. |
| W3C PROV | Neutral names for the evidence layer. | Its constraint set. |
| Theory-of-mind generators | A tracker of who witnessed what. | Short stories as the carrier. |
| Proper scoring | A score against a recorded chance. | Forecasts of the real future. |
| Anthropic's guidance on agent evals | Capability evals that start low and graduate when saturated, balanced cases, a reference solution per task, code graders where they fit. | Nothing. |

Six entries were fetched and read for this design: RECON, STALE, MemConflict, MemoryAgentBench, Veracium and Anthropic's guidance. Four are code and specs in this repository. The others are cited from the literature and were not fetched again.

MemoryAgentBench, LongMemEval and FrontierMemory stay outside calibration runs. This benchmark does not rebuild them.

## Build order

Each slice is useful alone. `tasks.md` gives each task's check.

1. **Slice 1: generated development projects and vault seeds.** The loader, the world core, seven mechanisms at one hop, three packs, the template renderer at three levels, the f38 projection with the `G` arm, and the vault seed. It builds the 60 entries marked `s1`, which cover f38's 11 dimensions and its stable probes.
2. **Slice 2: implied change and the model-free layer.** The four deeper implicitness levels with bridges, cascades, order, quantities and rules, the membench projection, the neutral export, the dials and the concordance check.
3. **Slice 3: minds, chance and adversaries.** Nested holders, the chance and causal models, the policy view, the model-prose renderer with its check, the independent review and the registration amendment.
4. **Slice 4: scale and reach.** The large world, the training export, media and other languages, and compound threads.

## Risks / Trade-offs

- **The catalogue encodes the home system's view of knowledge.** Four in five entries are home-defined. The neutral families, the lineage field, the independent review and the per-group report reduce the risk and do not remove it.
- **The oracle can be wrong.** A wrong mechanism yields confident wrong keys at scale. The mutation and invariance checks catch a key that ignores its plant. The ceiling arm catches a key that careful readers reject.
- **Templates can be pattern-matched.** A product could learn the surface banks. Disjoint banks per split, paraphrased probes and the optional model prose reduce it.
- **The world can feel synthetic.** A tidy world understates difficulty. The profile and the packs carry the realism, and the first rounds will show where they fall short.
- **Reference arms add cost.** `G` and `N` double the probe calls. They need no session play.
- **The generated family may never earn a decision.** If the concordance check fails and the cause is not found, generated worlds stay a development instrument. That is still the first slice's purpose.
- **Predictions can anchor the reader.** A filed prediction of failure may excuse a failure. The report therefore prints the position between floor and ceiling first.

## Open Questions

The owner answered on 2026-11-10:

1. **Standing.** Decision 9 stands, kept lean. The review, the concordance round and the amendment are one-time steps before generated worlds may decide, not gates on every run. The review is the adversarial review that every change gets, with the fairness brief. The concordance check is one development round. The amendment is a short registration in the existing protocol.
2. **Seed custody.** Evaluator custody outside the repository, as f38 uses.
3. **Audience.** Build the benchmark so that others could run it: product-neutral, self-contained and documented from the first slice, with the neutral export no later than slice 2. Whether to publish is decided after the first results and an outside fairness audit.

Still open, each decided before the slice that needs it:

4. Which budget covers the first outcome round: the capped low-cost actor, subscription turns or API credits? The round states its cost first.
5. May the model-prose renderer run in slice 3, given its cost, or should surface banks grow first?
6. Where do the large-world profile's shape numbers come from? The options are a private file, computed from a real vault and read at run time, numbers committed in the repository, or invented distributions. Decided before slice 4.
