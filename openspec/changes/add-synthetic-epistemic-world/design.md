## Context

The repository already holds most of the machinery that a generated benchmark needs.

- **membench** (`benchmarks/membench/`) has a strict record schema, a bitemporal oracle that derives every expected answer, a privacy-safe wordbank, native renderers with per-fact parity reports, and a runner. Its unit of authoring is the scenario template: a Python function that builds its own entities and writes each status span by hand.
- **The epistemic task suite** (f38, `benchmarks/membench/outcome/epistemic/`) plays an actor through a project of work sessions and scores probes with a blind judge. Its runner, case check, development gates and statistics exist. Its three projects are hand-written, in evaluator custody. Its own development round has not run yet.
- **The Epistemic State Bench** (`benchmarks/epistemic/`) has a frozen family and assertion registry, an amendment protocol and control twins.
- **The shipped vocabulary pack** (`src/exomem/vocabulary/packs/core/`) is the model for the catalogue: open words in YAML, and closed attribute sets that code implements.

Three earlier lessons bind this design.

- The 2026-08 audit of membench's head-to-head run found the comparison unsafe in both directions (`docs/memory-proof-benchmark.md`). Whoever writes a harness shapes its outcome, with or without a competitor in the frame, so the writer cannot vouch for its fairness.
- The 2026-08-09 rescope withdrew every cross-product comparison authored in this repository. That rule stands.
- A generated corpus predicts a cost only on the axes where its data matches real data. Elsewhere it can rank two changes and cannot predict an absolute number.

## Goals / Non-Goals

**Goals:**

- One catalogue that can hold hundreds of epistemic phenomena, with a structure that does not change as it grows.
- Every key derived in code from the evidence layer of a generated world, for every phenomenon the catalogue marks as computable.
- A world whose documents carry their statements implicitly and messily, with the truth still known.
- A reading that shows how far each arm sits from a floor and a ceiling, and how often it raises a false alarm.
- Slices that are each useful alone. The first one gives f38 a generated development project beside its hand-written one.

**Non-Goals:**

- A leaderboard, a single score or a published comparison of products.
- A replacement for the hand-written f38 projects. They stay the independent anchor.
- A migration of membench's 24 templates. They keep working as they are.
- The 50,000-page latency world and the sensors' training export. This design fixes their seams and builds neither first.

## Terms

| Term | Meaning |
|---|---|
| World | One generated universe from one seed: entities, events and the statements made about them. |
| Catalogue | The registry of phenomena, families, norms, recipes and their closed attributes. |
| Phenomenon | One catalogue entry, such as `currency.reversal`. |
| Recipe | The data that plants one phenomenon: roles, world operations and the readings that are its keys. |
| Thread | One planted instance of a phenomenon in a world. |
| Planted world | A thread with its phenomenon in place. |
| Mutation world | The same thread with its phenomenon left out. |
| Statement | One thing that a holder said, wrote, measured or decided, with its content, time, kind and basis. |
| Artefact | One rendered document that carries statements: a message, a note, a file, a table or a log. |
| Norm | A rule that the oracle applies and that the statements alone do not fix. |
| Warranted state | What the statements up to a date support, under the registered norms. |
| Oracle | The code that derives the warranted state and every key. |
| Reading | One named result of a truth mechanism, such as `state_fold.current`. |
| Probe | A task with a key. |
| Proof trace | The artefacts that carry the statements and the stated norms that a key rests on. |
| Contest set | A probe's proof trace, plus every artefact that holds one of its traps or a defeated statement. |
| Projection | An output of a world for one instrument or one product. |
| Round | One run of a set of arms over one world. |
| Dial | A difficulty setting that a round plan declares before the round runs. |

## Decisions

### 1. Truth comes first, in three layers

The generator builds a world in three layers, and text comes after all three.

1. **The world.** Entities, their attributes over time, the events that change them, the rules and causes that connect them. This layer is complete and consistent. No actor ever sees it.
2. **The evidence.** The statements that holders made: who said what, when, of what kind, how hedged, on what basis. A statement may be late, wrong, partial, biased or false, and the generator records how each one differs from the world.
3. **The warranted state.** What the evidence up to a date supports. The oracle derives it from the evidence alone.

**Keys come from the warranted state, not from the world.** Suppose the world changed and no statement recorded it. Then the warranted answer is the old value with its date. If the evidence supports no answer, the key is an abstention, although the world holds a value.

- **Rejected: keys from the hidden world.** That punishes a correct abstention and rewards a lucky guess.
- **Rejected: text first, keys extracted afterwards.** An audit that the Veracium paper cites found 6.4% of one benchmark's answer keys corrupt. STALE's scenarios are model-written and judge-validated, so their truth is asserted, not constructed.
- **What would change this:** the full-history arm disagreeing with the keys on more than one probe in ten in a family. Then that family's norms are wrong, or its entries belong on the rubric track.

**Every norm that a key rests on is registered.** Statements alone do not fix a key. The oracle also applies norms, such as that a holder's later statement replaces that holder's earlier one. `catalogue/norms.yaml` lists all 22, and the oracle applies no other.

- A **stated** norm appears in a document of the world, and the proof trace holds that document. Which record governs a fact is always a stated norm. So is every causal rule, every chance and every claim that a list is complete.
- A **background** norm appears in no document. A careful reader is assumed to share it. Sixteen of the 22 norms are background norms.
- Of the 473 entries with a computed key, 357 rest on at least one background norm, 81 on stated norms only and 35 on none.
- A report prints, for each background norm, the number of keys that rest on it and the share that the full-history arm reaches. A norm that careful readers do not share shows up there, and its entries move to the rubric track.

The oracle never reads the generator's own model of events. `causal_model` and `chance_model` apply only the causal rules and the chances that the world's records state, and they count only recorded outcomes. `state_fold` takes each attribute's volatility class, so a momentary state and a permanent fact age differently, as Veracium's classes do.

### 2. The catalogue is open, and everything that code implements is closed

This is the shipped vocabulary's rule, applied to the benchmark: open words, closed behaviours.

- **Open:** phenomena, families, behaviours, norms, statement kinds, recipes, domain packs, surface forms and bridges. Each is a YAML entry. Adding one is a data change.
- **Closed:** truth mechanisms and their readings, world operations, rule shapes, role kinds, key parts, key classes, probe forms, implicitness levels, volatility classes, scorers and dials (`catalogue/attributes.yaml`). Code implements each member. Adding one is a code change and a schema change.

Code never branches on a phenomenon id, a family key, a behaviour key or a norm key. A phenomenon is a composition of closed parts. The 512 entries need 13 pipeline mechanisms, 14 probe forms and 16 world operations.

**A recipe is data.** `catalogue/recipes.yaml` holds one recipe for each buildable phenomenon. A recipe holds no condition, no loop and no expression.

```yaml
currency.reversal:
  needs:
    owner: {is: holder}
    item: {is: attribute, volatility: durable}
    old: {is: value, of: item}
    new: {is: value, of: item, differs_from: old}
  slots: [before, change, probe]
  steps:
    - {op: set, target: item, value: old, at: before}
    - {op: state, id: first, by: owner, about: item, value: old, kind: decision, at: before}
    - {op: set, target: item, value: new, at: change, world: planted}
    - {op: state, id: second, by: owner, about: item, value: new, kind: decision, at: change, world: planted}
  keys:
    state: {at: probe, value: {read: state_fold.current, of: item}, traps: [{read: state_fold.replaced, of: item}]}
    premise: {at: probe, premise: {read: state_fold.replaced, of: item}, value: {read: state_fold.current, of: item}}
```

- `needs` names roles. The planner binds each role to a thing in the world that meets its constraints.
- `slots` orders the thread's moments. The round's distance dial places them on the session timeline.
- `steps` is a straight list of world operations over the roles.
- `keys` names, for each probe form, the readings that give the value, the traps, the premise and further facts for the judge.

**Several mechanisms apply in one fixed order.** 214 entries name two or three mechanisms. An entry lists them as a set, and `pipeline_order` fixes the order in which they apply. Each mechanism narrows or annotates the statements that the later ones read. The recipe then names the one reading that is the key. In `entities.who-to-ask`, a later statement uses an entity's short name. `identity_map` resolves that name first. `state_fold` then folds both statements about the duty, and the key is `state_fold.current`.

**What the desk-check showed.** Slice 1 started with a desk-check, before any code. This change now ships 46 recipes: the 26 that slice 1 needs, and 20 for later slices, at least two in each realm. 21 came with the first catalogue, and the desk-check of 2026-11-11 wrote 25. A scratch check, which is no part of the repository, found that every recipe names only declared closed values.

- **The falsifier did not fire.** No recipe of the 46 needed a new mechanism or a new operation. The limit was a quarter of them.
- **Two closed values were forced.** `correction.user-correction` and `self.earlier-session` need the holder whom the actor works for, so `user` is a ninth role kind. `deception.injected-instruction` now binds its owner to it. `uncertainty.hedged-claim` needs a value for the `hedge` argument, so `hedges` is a new closed set of two members. The 25 recipes added no reading and no argument. The first 21 had added four readings and three operation arguments, and had removed one rule shape.
- **Two operations left the set.** The recipes use 16 of the 18 operations. `omit` duplicated a `state` step that applies in one world only. `split` found no recipe among the 46, and it returns with the recipe of `identity.split`, as a code change and a schema change. `correct`, `end`, `merge` and `remove` each found a recipe.
- **Three meanings were sharpened, with no new value.** A statement of a kind that commits its holder to nothing does not count for `corpus_closure.recorded`. A `correct` step replaces a statement's content for the whole period that it covers. A replaced statement supports nothing, as a withdrawn or defeated one does.
- **Every recipe now keys every probe form that its entry declares.** Five recipes left a form unkeyed: `order.sequence`, `argument.rebuttal` and `calibration.known-chance`, and the twin parts of `propagation.dependent-update` and `deception.injected-instruction`. Two of them gained a key, and three entries lost a form. Six of the new recipes also changed their entry's forms, where the family's default asks a question that the phenomenon does not raise: five dropped a form, and `uncertainty.hedged-claim` asks `act` in place of `abstain`.
- **Six entries named the wrong norms.** Three propagation recipes rest on support edges and on no stated rule. `scope.one-client` rests on a scoped decision and on no stated rule. `quantity.missing-term` rests on a stated dependency that its entry did not name. `machine.gap-not-zero` rests on absence and volatility, and its family's default names neither.
- **A role trap is right in the mutation world.** A trap such as "done" is the content that the plant leaves unestablished. With the phenomenon left out, that content is no error, so decision 3 now drops role traps from a mutation-world key.
- **Still unexercised.** No recipe keys the probe form `as_of`. No recipe uses the rule shapes `implies` and `volatility`, or the arguments `set.scope`, `restrict.until` and `copy.altered`. 18 of the 52 pipeline readings are never read. The desk-check removed operations only, as this decision asks.
- **What it does not show.** No recipe has been planted, so no key has met the mutation, invariance and uniqueness checks. `identity.rename` and `identity.alias` key the referent of the new name. A task that needs a fact filed under the other name has no key yet, because a key cannot say which name the probe uses.

- **Rejected: one template function per phenomenon.** That is membench today. It puts vocabulary in code and does not reach hundreds of entries.
- **Rejected: a rule language in the catalogue.** Data that defines its own truth rules turns a vocabulary into a programming language. The epistemic-language design rejected the same move. A recipe with conditions would be that language under another name, so the recipe form has none.
- **What would change this:** more than a quarter of the 46 desk-checked recipes needing a new mechanism or a new operation. Then the closed sets are wrong, and the catalogue's structure does not hold. The desk-check found none in 46. The first family of each later slice repeats the reading, because 26 of the 46 belong to slice 1.

### 3. One coherent world, with planted threads that do not interfere

A world has a backbone and threads.

- **The backbone** is ordinary life in one domain pack: people, teams, systems, suppliers and routines. It produces the stable facts, the background documents and the distractors. Its shape comes from a profile: how many entities, how busy each one is, how long documents are, how often a fact is restated.
- **A thread** plants one phenomenon on backbone entities, by its recipe. The planner plants a thread only when the world holds what the recipe needs, as RECON's production rules do.
- **The planner reserves** every attribute that a thread's key reads. No other thread may change a reserved attribute. Each probe therefore depends on one planted change, which f38 requires.

**Each thread has two worlds.** A recipe step may apply in the planted world only, or in the mutation world only. The mutation world is the thread with its phenomenon left out. It has three uses.

- **The mutation check.** A probe depends on its plant when its value differs between the two worlds. It also does when it has a trap in the planted world and none in the mutation world. A probe that meets neither condition is refused. An abstention probe meets the first condition: its mutation world holds the statement that answers it.
- **Twins at no authoring cost.** The `twins` dial sets the share of a family's threads that the planner plants in the mutation world. They ask the same probe forms, and the right answer reports no change, no conflict and no doubt. The key of such a thread keeps its reading traps and drops its role traps, because the content that a role trap names is no error where the phenomenon is left out.
- **Near-miss twins.** A declared twin entry may instead key a case that the plant does not touch. `scope.general-rule-applies` keeps the exception planted and asks about a case outside it.

Compound threads, which combine two phenomena on purpose, come in slice 4 and declare both.

- **Rejected: scenario islands.** No island shares an entity with another, so nothing interacts and no distractor is natural.
- **Rejected: a free simulation of agents.** Its events are uncontrolled, so no phenomenon is guaranteed to occur and no key is guaranteed to be unique.
- **What would change this:** reservation leaving too few free attributes to plant 30 threads in a 12-session world. The first answer is more entities per pack, not another design.

### 4. Extend membench, and reuse its oracle

The generator is a subpackage, `benchmarks/membench/world/`. It reuses the wordbank, the id scheme and the outcome suites. Its membench projection emits `EntityRecord`, `SourceRecord`, `ClaimRecord`, `QueryRecord` and `ExpectedRecord`, so the existing runner, adapters and gates read a world without change.

`state_fold` does not implement time resolution again. It turns statements into membench `StatusSpan` records and calls membench's `truth_at`, so the bitemporal rule lives in one place. membench's span lint runs on the derived spans from slice 1. It is a free check that the world's authors did not write.

The gap that needs new code:

| membench today | The world adds |
|---|---|
| Templates write each status span by hand. | `state_fold` derives each span from statements, and membench resolves and lints it. |
| Each template owns its entities. | One backbone, shared by all threads. |
| A source asserts, disputes or retracts. | A statement also has a holder, a kind, a hedge, a basis and a scope. |
| Text states each fact in plain template lines. | Seven implicitness levels. |
| Output is sources and queries. | Work sessions with probes, store seeds, and later a large world and a training export. |
| Families and statuses are Python constants. | The catalogue is YAML. |

- **Rejected: a new top-level package.** It would copy the ids, the wordbank, the schema and the runner.
- **Rejected: depend on Veracium's generator.** It is MIT-licensed and builds facts before text, which is the right shape. It emits a personal chat and email script with 15 question types. It has no holders' beliefs, kinds, arguments, dependencies or scopes, and no work sessions. It needs a newer Python, and a memory product's team maintains it.
- **Rejected: adopt RECON's generator.** The paper states no code licence, and this design could not open its anonymous repository. Its cases are single long case files.
- **What would change this:** a licensed RECON release that emits session streams, or a Veracium release that covers most of the mechanisms. Either would replace the planner, not the catalogue.

`membench/world/` imports nothing from `exomem`, and a test asserts it. Code that writes into a product, such as a vault seed, lives beside membench's native renderers, outside that package.

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
- **A bridge** links an attribute to the observations that carry it. Each bridge declares its strength: `entails` or `suggests`. Only an `entails` bridge yields a computed key. A `suggests` bridge yields a rubric-track probe.
- **Entailment is constructed, not audited afterwards.** An `entails` bridge names the norm that leaves the observation no other reading. That norm is a rule that a document of the world states, or a registered background norm. The `silent` level always needs a stated norm, such as a document that declares a list complete. The loader refuses a computed entry at the `silent` level that names no stated norm.
- **The full-history arm audits every bridge** on development worlds. A bridge whose probes that arm fails is unfair, and the pack drops it.
- **Probes use their own surface forms.** No probe shares wording with the artefact that answers it. An audit of membench found the opposite defect: a renderer that phrased facts in the question's vocabulary.

The level and the bridge of every artefact go into the world's manifest, so a report can split results by level.

**Model-written prose is optional and gated.** Templates are the default renderer and cost nothing. A model may rewrite an artefact for natural, messy prose. Then a check must pass in both directions:

1. **Anchors.** Every name, value and date of the planted statements is present. No trap value appears in an artefact that must not hold it. No explicit form appears in an artefact rendered below `stated`.
2. **Round trip.** A model of another family reads the rewritten artefact blind and lists its statements through a structured-output schema. Every planted statement must come back, and nothing may come back that the manifest lacks.
3. **Fallback.** A note that fails twice keeps its template rendering, and the manifest records that.

The check itself is tested by mutation: the suite drops and adds known facts and counts how many the check catches. The key never reads rendered text. A world with model prose stores that prose, and its manifest pins each artefact's hash. A later run reads the stored prose and never writes it again.

- **Rejected: templates only, for ever.** RECON's authors note that a mechanically written file can be solved by pattern matching.
- **Rejected: a presence-only check.** Veracium's verifier finds a missing fact and not an added one, and its paper names that as a limit.
- **What would change this:** a round trip that cannot catch nine planted mutations in ten at a bearable cost. Then model prose stays off, and the surface banks grow instead.

### 6. Every family has a floor and a ceiling

A score alone says nothing. Each round plays reference arms beside the memory arms.

| Arm | What the actor has | Role |
|---|---|---|
| `N` | No memory and no history. | Floor. |
| `G` | No memory. The probe's contest set. | Ceiling. |
| `H` | No memory. The full text of every earlier session. | Gate on development worlds. This is f38's solvable gate. |
| `F` | A plain memory folder, with the product's learning instruction. | Baseline store. |
| `P` | Exomem as shipped. | Primary. |
| `E` | Exomem over MCP alone. | Ablation. |

**`H` gates the keys.** `G` cannot check a key, because the oracle chooses what `G` reads. A key that ignored a contrary statement would pass `G` unseen. `H` reads every earlier session, so it meets every contrary statement. On a development world, `H` must solve at least 90% of the probes. Each probe that `H` fails is repaired or dropped, with the recipe, surface form or bridge behind it. The report records the repair rate.

**`G` is the ceiling, and it still has to resolve the contest.** `G` receives the probe's contest set. That is the proof trace, plus every artefact that holds one of the probe's traps or a statement that the key's norms defeat. The artefacts come in session order with their dates. `G` skips the search and keeps the epistemic work. In the worked thread below, `G` reads the old decision, the changed checklist and the later report of the old value.

**A probe form is read in one of two ways.** Each form declares its reading in `attributes.yaml`.

- **Position.** (arm − `N`) / (`G` − `N`), with the paired differences and their intervals beside it. Twelve forms read this way.
- **Harm.** A no-memory arm answers an `abstain` or `unaided` probe correctly, so those forms have no floor. Their reading is the share of probes that `N` gets right and the arm gets wrong.
- The memory-needed gate applies to position probes only: `N` must fail at least 80% of them, and each one that `N` passes is repaired or dropped.

**Over-flagging has its own number.** A family's position is printed with its false-alarm rate: the share of its mutation-world and twin probes on which the judge finds `over_flags`. The first value of the `twins` dial is one thread in four. An arm that flags everything gains position and loses here.

**A small count reads as a small count.**

- A family's position is printed only when one window holds at least 20 position probes for it. Below that, the family reads `too few probes`, with its raw counts.
- With enough probes, a family reads `no room` when the upper bound of the interval of `G` − `N` is below 0.2. It reads `room` when the lower bound is at least 0.2, and only then is a position printed. Between the two it reads `room unclear`.
- A family reads `saturated` when the lower bound of the interval of `P` − `G` is above −0.05 on two reports in a row. It then becomes a regression check, and the next round plan raises a dial for it.
- Slice 1's project holds two to four probes per family. Its report prints one pooled position over all its change probes, and raw counts per family.
- A per-family position needs a round that samples few families deeply. Generation is free, so such a round costs only its model calls.

A round plan declares each dial's value for each family before the round runs. No dial changes after a held-out result. No score spans rounds or the catalogue. A family that a round did not sample reads `not measured`, never 0.

- **Rejected: a table of accuracies.** A home benchmark at 100% tells nobody anything, and one at 40% does not say whether the ceiling is 45 or 95.
- **Rejected: `G` on the proof trace alone.** Its currency ceiling would be reading one number from one file.
- **What would change this:** nothing in view. The numbers 20, 0.2, 0.05 and one in four are first values. The first two development rounds may move them before any held-out world exists.

### 7. Predictions are filed before a run

`projections/exomem.yaml` states, per family, whether Exomem models it and what this design expects: pass, partial or fail. The file sits outside `catalogue/`. A manifest pins the catalogue's digest and the projection's digest apart, so an edited prediction changes no world. The report prints each prediction beside its result.

The first register expects no family to pass. It expects 13 of 35 to fail: persistence, source reliability, calibration, propagation, scope, quantity, statistics, meaning, inference, causation, belief states, deception and machine sources. It expects the implied and propagated levels to fail in every family. These are readings of the shipped pack and the active specs. No run backs them.

A wrong prediction is a finding in either direction. A family that passes against its prediction needs its probes read before anyone believes the pass.

### 8. Splits are separate worlds, and they differ in more than names

| Split | Seed | Use |
|---|---|---|
| `train` | Public. | Labelled spans for distilling the model-based sensors. Never scored. |
| `dev` | Public. | Iterating the generator, the harness and the product. |
| `heldout` | In evaluator custody. | One decision. Then the world is retired to `dev`. |

- A world's seed derives from the master seed, the split and the world's index. The split is part of every identifier.
- Names, surface forms, bridges and work-task templates each belong to one split, by a stable hash of their ids.
- Before the first decision, one domain pack is held out. No `train` or `dev` world uses it.
- A loader refuses to mix splits. The training export refuses a world that is not `train`. A sensor's training manifest lists the digests of its inputs, and a round refuses a sensor trained on a `dev` or `heldout` digest.
- A component trained on the training export is never decided on generated worlds. Its decision reads hand-written or real cases.
- The repository holds a commitment to each held-out world: the digest of its seed and of its manifest. The seed itself never enters the repository, a prompt or a fixture.
- A held-out world is generated only from a tagged generator. No probe of it is repaired or dropped after its run. The development split's repair rate is the stated estimate of its flawed probes.

**What the splits still share.** The catalogue, the recipes and the mechanisms are the same in every split, because they are what the benchmark measures. A product tuned to a recipe's structure on `dev` carries that tuning to `heldout`. The held-out pack, bridges and templates change the carrier and not the phenomenon. The hand-written f38 projects stay the cases that this tuning cannot reach.

- **Rejected: one world, split by probe.** Probes of one world share entities and facts, so a held-out probe leaks through a development one.
- **Rejected: a fixed held-out bank.** Every reuse spends it. A generator makes a fresh one for each decision.

### 9. Generated worlds decide nothing until an independent lane has attacked them

The generator, the catalogue and the Exomem projection come from Exomem's own lanes. That is the bias the fairness finding describes.

1. Slice 1 feeds **development rounds only**. The hand-written f38 projects stay the cases that an f38 decision reads.
2. Before generated worlds decide anything, a lane that wrote none of this reviews the generator, the catalogue, the surface banks and the projections. Its brief is to find every way they disadvantage the folder arm or another product.
3. A **concordance check** plays the hand-written development project and a generated one in the same window. It compares the paired difference between `P` and `F` over all change probes of each project. The two disagree when their intervals do not overlap. Generated cases are then suspect until the cause is found.
4. A preregistration amendment then registers generated worlds as their own family, with their decision rules. The existing protocol withholds the family until the owner acknowledges the receipt.

The check is one development round, on a generated world that the earlier repairs have reached. One project on each side detects only a large disagreement, and the amendment states that limit. The check never compares single dimensions, where two probes on each side would decide by chance. Slice 1's first round already plays both projects, and its comparison is description.

- **Rejected: replace the hand-written held-out projects now.** They are the only cases that the generator's authors did not shape.

### 10. A judge decides free text, and code scores structure

f38's rule stands: a blind judge decides whether a free-text answer is correct, from the task, the key and the answer. The judge is the strongest available model, from another family than the actor where one is available, and a second-family sample re-judges for description.

Where a probe asks for a structured field, code scores it: an exact value, a set, an order by Kendall's tau, or a probability by the Brier score. A judge is a poor instrument for an order or a probability.

A key carries the value, its accepted forms, a list of traps and the premise of a premise probe. It also carries the behaviours, the facts that the judge needs, the proof trace and the contest set. Accepted forms come from typed formatters and the identity map, never from a system's output.

**f38 changes in four places.** Its case check refuses what a generated project needs. Task 1.3 agrees these changes with the change that owns the suite.

1. **Behaviours come from the registry.** f38's judge rates six behaviours from a code constant, and its case check refuses any other. The catalogue names 21, and 15 occur in slice 1. The judge and the case check will read `behaviours.yaml`, whose first six entries are f38's own. That retires the constant. The judge's schema for a probe holds only the behaviours that the probe's key names, plus `trusts_stale`. The schema requires every behaviour that it lists on every verdict, and the report reads only those.
2. **A premise is keyed apart from a trap.** A premise probe's task must presuppose the stale or false state. The key holds that state in `premise`. The case check requires the premise in the task, and applies the leak rule to the value, the accepted forms and the traps only. The judge sees the premise beside the value and the traps, and rates an answer that relies on the premise as `trusts_stale`. f38's judge sees a value, accepted forms, a rubric and one trap today.
3. **A key holds a list of traps.** A value that changed three times has two traps. A hand-written project's single trap reads as a list of one.
4. **A key names its reading.** The memory-needed gate skips a probe that is read as harm.

### 11. The world's text is product-neutral, and the catalogue's shape is not

**What is neutral.**

- The evidence layer uses the W3C PROV names: entity, activity, agent, derivation and attribution. The statement kinds are the world's own (`catalogue/statement-kinds.yaml`). The world uses no Exomem kind, relation or dimension.
- The Exomem projection is the only file that maps the world to a product. It maps a family to the dimension key that f38 reports.
- Every projection writes a parity report, as membench's native renderers do: each fact is represented, degraded or unsupported.
- Arm `F` receives the same sessions, files and learning instruction as `P`.
- When a round seeds its stores, `F` receives the neutral artefacts as a plain folder of files. An Exomem vault without typed structure is a third seed, and it never stands in for the folder.
- A neutral export, built in slice 2, lets another product's own team run a world. This repository authors no competitor integration and publishes no cross-product table.

**Three gates run on the adapter side.**

1. **Parse.** The neutral artefacts, parsed by each product's own grammar, must yield no typed unit and no typed relation.
2. **Banned words.** membench's banned-vocabulary lint runs over every artefact and every probe. It refuses a term that only one product uses, such as a tool name.
3. **Overlap report.** A report generated from Exomem's vocabulary pack counts the artefacts and probes that hold each shipped label and key. Ordinary words such as "decision" will occur, so the report counts and does not refuse. A surface bank replaces a term whose count stands out.

**What is not neutral, and how the report shows it.**

- Twelve families mirror Exomem's shipped dimensions. Eleven use a dimension's key, and `propagation` covers the `dependency` dimension. They hold 200 of the 512 entries, and 25 of the 30 entries planned for slice 1. `families.yaml` marks them with the lineage `exomem-dimensions`.
- Six entries came from Exomem's own design rules. They carry the lineage `home-design`, and their keys are now outcomes that the world fixes, not behaviour that one product prescribes.
- 101 entries name an outside work as lineage. The other 411 come from this project.
- Three entries depend on what the arm itself did or held. They have the key class `run-relative`, and a report gives them as harm or description, never as a position.
- A report states the share of its probes that come from each of these groups, so a reader can discount the home-shaped ones.

### 12. Cost is designed in

| Layer | What runs | Model usage |
|---|---|---|
| 0 | Generation, the oracle's invariants, projections and leak checks, in CI. | None. |
| 1 | A product over the membench projection, with deterministic gates. | None. |
| 2a | An outcome round with the pinned low-cost actor of the utility instrument. | Metered, capped at $2 a round. |
| 2b | An outcome round with the subscription actor, for a decision. | Subscription turns. |

- A round is small: one world, a declared set of families, and four to six arms.
- **A development round costs a full f38 round today.** `F` and `P` play all 12 sessions. f38's `N` plays them too, although nothing of a session survives for it. `G` and `H` play probes only. Task 4.4 lets `N` play probes only. Until then, `N` is not a cheap arm.
- A development round uses a fresh world, so a repair is checked on the cases that exposed it and judged on new ones.
- Every paid run states its cost and the question it answers before it starts. The f38 estimate of $20 to $25 for a full development round is unverified, and this design repeats it only as that.
- Floor and ceiling results are not reused across time windows. A provider's behaviour moved within hours on 2026-11-08, so all arms of a decision run in lockstep.

## The catalogue

`catalogue/` holds the first catalogue as data.

| File | Holds |
|---|---|
| `attributes.yaml` | The closed sets. They include 13 pipeline mechanisms with their readings, plus `run_record`, `judged` and `none`; 16 world operations; 9 rule shapes; 9 role kinds; 4 key classes; 14 probe forms; 7 implicitness levels; 5 volatility classes; 6 scorers; 8 dials. |
| `behaviours.yaml` | 21 behaviours that a judge rates, each wanted or unwanted. The first six are f38's. |
| `norms.yaml` | 22 norms that a key may rest on: 6 stated and 16 background. |
| `statement-kinds.yaml` | 20 kinds of statement, each with how far it commits its holder. |
| `families.yaml` | 9 realms and 36 families, each with its question and its default attributes. |
| `phenomena/<realm>.yaml` | 512 entries. An entry states what differs from its family's defaults. |
| `recipes.yaml` | 46 recipes: the 26 that slice 1 needs, and 20 for later slices. |
| `lineage.yaml` | 36 sources, each with what this benchmark adopts and leaves, and how it was checked. |

`projections/exomem.yaml` sits beside the catalogue and holds the Exomem dimension and the filed prediction per family.

An entry reads:

```yaml
- {id: propagation.independent-support, stage: s2, says: "A conclusion loses one support and survives on another.", from: [recon, agm]}
```

It inherits the family's mechanisms (`state_fold`, `warrant_graph`), key class, norms, probe forms (`impact`, `act`) and behaviours.

Size by family, counted on 2026-11-11 by loading every file in `catalogue/phenomena/`:

| Realm | Families and entries |
|---|---|
| Time and change (48) | currency 18, order 18, persistence 12 |
| Sources and testimony (63) | provenance 18, attribution 16, reliability 15, testimony 14 |
| The status of a statement (60) | kind 19, modality 13, uncertainty 16, calibration 12 |
| Conflict and revision (61) | correction 16, conflict 15, argument 15, propagation 15 |
| Reference and scope (91) | identity 16, entities 16, scope 17, quantity 18, statistics 12, meaning 12 |
| Inference (46) | inference 18, causation 14, absence 14 |
| Minds and society (63) | belief 14, consensus 11, commitment 15, deception 13, disclosure 10 |
| The corpus and the memory (55) | hygiene 12, self 11, procedure 12, media 12, language 8 |
| Beyond human sources (25) | machine 18, limits 7 |

By planned slice: 30 in slice 1, 242 in slice 2, 192 in slice 3, 41 in slice 4, and 7 declared out of scope. By key class: 473 computed, 29 on the rubric track, 3 run-relative and 7 out of scope.

**The key classes are still a first assignment.** The first review moved 16 entries to the rubric track and 3 to the run-relative class, and removed 4. It reworded 50 entries. Some now have a document of the world state the rule that their key applies. Others now key an outcome of the world in place of a product's behaviour. An entry's class is confirmed when its recipe is written, and again when the full-history arm reads its probes on a development world. The desk-check read the 54 entries that the 46 recipes key, eight twins among them. In each, one reading fixes the answer, so none moved to the rubric track. `persistence.transient` keeps its class on one condition: the planner places the probe beyond the span of the attribute's volatility class.

**A slice label is a plan, not a gate.** The loader computes which entries are buildable. An entry is buildable when it has a recipe and the generator implements every closed value that the entry and the recipe name. A round plan may name only buildable entries.

**How the catalogue reaches thousands of cases.** A case is a phenomenon crossed with the round's settings. Distance, distractors, restatements and twins multiply cases with no authoring. Three axes need authoring, and the section on size counts them: a recipe for each phenomenon, a surface bank for each pack and split, and bridges for the four deeper levels.

## Generator architecture

```
catalogue + domain pack + profile + seed
        │
        ▼
  backbone ──► planner ──► world ──► evidence ──► oracle ──► warranted state, keys, proof traces,
                 (threads, both       (statements)              contest sets
                  worlds,                                          │
                  reservations)                                    ▼
                                             renderers ──► artefacts (level, bridge, surface form)
                                                                   │
                                                                   ▼
                                    projections: f38 project · membench corpus · store seeds · …
```

1. **Load.** The catalogue loader validates every closed value and every reference, and lists the buildable entries. An unknown value stops the load with the entry's id.
2. **Backbone.** The domain pack and the profile build entities, routines and stable facts. Names come from the wordbank's partition for the split.
3. **Plan.** The planner draws threads for the round's families and binds each recipe's roles. It decides which threads take the mutation world, reserves attributes, and places slots and probes on the session timeline. A probe never shares a session with an artefact that holds its answer.
4. **World and evidence.** Recipes run as world operations. A validator checks the world's invariants: times in order, one holder per role at a time unless stated, sums that add up.
5. **Oracle.** The entry's mechanisms run in pipeline order at each probe's date. The oracle evaluates the recipe's readings and writes the key, the proof trace and the contest set. It checks that each key is unique.
6. **Render.** Each statement gets a surface form at the round's implicitness level. The anchor check runs on every artefact.
7. **Project.** Each projection writes its output and its parity report.
8. **Manifest.** The manifest pins the generator version, the catalogue digest, the projection digest, the pack digests, the seed commitment and every artefact hash. Generated output is never committed.

Steps 1 to 8 use no model and are deterministic: the same inputs give the same bytes. Model-written prose is the one exception, and decision 5 says how a world freezes it.

A world for one f38 project is small: 12 sessions, about 30 threads and a few hundred artefacts. The same pipeline at a larger profile gives the latency world.

**Domain packs.** Slice 1 ships one pack: a small software and operations team. Slice 2 adds a research and data project and a small business's operations, which are f38's other settings. A pack is YAML: entity types, attribute schemas with value generators and volatility classes, event types, artefact kinds, work-task templates, surface forms and bridges. Later packs add settings that other families need, such as a field station with instruments for machine sources.

**A worked thread.** `currency.reversal` in the operations pack, at level `unmarked`:

1. The planner binds `item` to `approvals(staging deploys)`, `owner` to the team lead, `old` to 1 and `new` to 2. It reserves the attribute.
2. The recipe sets the value to 1 in week 2 and to 2 in week 6. The lead decides both.
3. The restatements dial adds one artefact in week 8: a contractor reports the lead's old rule from memory. A report asserts nothing, so `state_fold` does not fold it.
4. `state_fold` applies the background norm that a holder's later decision replaces that holder's earlier one. It gives 2 as current from week 6, and 1 as replaced.
5. The renderer writes the week 6 statement as a changed line in a checklist file, with no word of change.
6. Session 10 asks the actor to prepare a staging deploy. Its files state neither number. The key is 2 and the trap is 1.
7. The proof trace is the checklist file. The contest set adds the week 2 note and the contractor's message.
8. In the mutation world, steps 2 and 3 keep only week 2. The key is 1 and there is no trap, so the probe depends on its plant.

## How each mechanism knows the truth

| Mechanism | It reads | It derives |
|---|---|---|
| `corpus_closure` | The complete list of artefacts, authors, dates and copies. | Whether anything is recorded, what is missing under a stated rule, and what is filed twice. |
| `identity_map` | Names, aliases, merges, splits and role holders over time. | The referent of a name at a date. |
| `interval_order` | Event times with their recorded precision. | Four-valued order, overlap, duration, computed dates. |
| `scope_lattice` | The contexts where each statement holds. | Whether a rule applies to the probe's case. |
| `holder_ledger` | What each holder said, saw and was told, and when. | Whose claim it is, who knows it, what one holder thinks another believes. |
| `kind_tag` | The kind, modality and hedge of each statement. | The status that an answer must keep. |
| `state_fold` | Asserted statements per attribute, the entry's norms and the attribute's volatility class. | Status spans. membench's `truth_at` then gives the current value, the history and the value at a date. |
| `warrant_graph` | Support, derivation and defeat edges that the recipes record. | What stands, what falls after a change, what survives on other support, the independent origins. |
| `rule_closure` | The rules and facts that the world's documents state. | Every conclusion that follows, with its steps. |
| `quantity_calc` | Typed values with unit, precision and error. | A computed value with its tolerance, a count, and the terms that are lacking. |
| `causal_model` | The causal rules that the world's documents state. | A recorded cause, and what a stated rule gives after a change or under another event. |
| `chance_model` | The chances that the world's documents state, and the recorded outcomes. | The stated chance, an observed rate, and the score of a stated probability. |
| `policy_view` | The audience and deletion statements in the world's records. | What the asker may see, and what must be withheld. |

The table lists the mechanisms in pipeline order. `run_record` is no part of the pipeline: the harness computes a run-relative key from the run's own record.

Three checks guard the oracle, and none uses a model.

- **Mutation.** Each probe must depend on its plant, by the rule in decision 3. A recall control has no plant. It must instead keep its key in every other thread's two worlds.
- **Invariance.** Adding an unrelated statement, or renaming every entity consistently, must leave every key the same up to the renaming. For `warrant_graph`, withdrawing one support must leave standing every conclusion that has another, as AGM's minimal change requires.
- **Uniqueness.** A probe whose key is not unique under its mechanisms is refused at generation.

Rubric-track entries have no computed key. The oracle supplies the facts, a predeclared rubric cites them, and a blind judge applies it. Out-of-scope entries generate nothing. They state where the benchmark stops.

## Projections

- **f38 project (slice 1).** `project-<x>.json` and `SPLIT.json` in the suite's case format, with the four changes of decision 10. A probe's dimension key comes from the Exomem projection. A family with no Exomem dimension keeps its family key as the probe's label, and its rows are description only.
- **membench corpus (slice 2).** Records in membench's schema, for the model-free layer.
- **Store seeds (slice 2).** The warranted state at a date, in three forms: a plain folder of the neutral artefacts, an Exomem vault with typed structure, and an Exomem vault without it. The vault writer lives beside membench's native renderers. The Exomem projection lists the display marks that each unit should carry, and a model-free check reads the marks back.
- **Neutral export (slice 2).** Sessions and documents as plain JSON, for another product's own harness.
- **Large world (slice 4).** The same pipeline under a size profile. The profile names each cost-driving axis: link density, document length, folder spread and duplicate rate. Its values are aggregate shapes only.
- **Training export (slice 4).** Text spans from `train` worlds with their labels from the evidence layer.

## What the build costs

One number in this section is measured: the rate at which a lane drafts recipes. No measurement backs the others.

- **Recipes.** The 473 computed entries need 458 recipes, because the 15 twins use their base's recipe. 46 exist. The 29 rubric-track entries need rubric lines instead.
- **The measured drafting rate.** The desk-check of 2026-11-11 drafted 25 recipes in about 18 minutes of one lane's session clock, which is about 80 recipes an hour. That time holds the design of each recipe, the entry edits and a scratch check of closed values. It leaves out the reading of this design. It is one measurement, of one model lane, on recipes that mostly reuse the shapes of the first 21.
- **What slowed the drafting.** Five things did, and none was the writing. An entry whose error is "cannot find it" needs a mutation world that changes a value, and the identity entries took the longest. A family's default probe forms did not fit six of the 25 entries. A family's default norms did not fit five. Three recipes needed a closed value that did not exist. A role trap needed a rule for the mutation world.
- **What the rate does not cover.** At that rate, a draft of the 412 remaining recipes is about five lane-hours. A drafted recipe is not a working one: it must still plant in each pack and pass the mutation, invariance and uniqueness checks. No measurement backs the time for that, and task 6.3 records it for the first family of slice 2.
- **Surface forms.** One form serves one pack, statement kind, level and artefact kind, in one split. Slice 1 writes the `dev` bank of one pack at three levels: about 200 forms, at eight kinds, three artefact kinds and three variants each. Each further pack, level and split is that work again. No measurement backs a rate for them.
- **Bridges.** Each is written for one attribute of one pack, with the norm that makes it entail. The four deeper levels need them from slice 2. No measurement backs a rate for them.

The claim that the other axes multiply the catalogue for free was wrong for surface forms and bridges, and this section replaces it.

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

Six entries were fetched and read for this design: RECON, STALE, MemConflict, MemoryAgentBench, Veracium and Anthropic's guidance. Six are code, specs and design rules in this repository. The others are cited from the literature and were not fetched again.

MemoryAgentBench, LongMemEval and FrontierMemory stay outside calibration runs. This benchmark does not rebuild them.

## Build order

Each slice is useful alone. `tasks.md` gives each task's check.

1. **Slice 1: one generated development project beside the hand-written one.**
   - A desk-check came first, and it is done: 46 recipes as data, before any code. 26 serve slice 1, and 20 serve later slices across the nine realms.
   - Then the loader, the world core, one pack, the template renderer at three levels and the f38 projection with its four changes.
   - Seven mechanisms, each in its smallest form: `state_fold`, `kind_tag`, `holder_ledger`, `identity_map`, `scope_lattice` with one level of nesting, `warrant_graph` at one hop, and `corpus_closure` for whether anything is recorded. f38's 11 dimensions and its abstention behaviour need all seven.
   - The 30 entries marked `s1`: two change entries for each of f38's 11 dimensions, three twins, two recall controls, two absence entries and one task that no memory bears on.
   - The first paid round plays the hand-written f38 development project and one generated project in one window.
2. **Slice 2: implied change, the model-free layer and seeds.** The four deeper implicitness levels with bridges, cascades, order, quantities and rules, the membench projection, the store seeds, the neutral export, two more packs and the split partitions.
3. **Slice 3: minds, chance and adversaries.** Nested holders, stated chances and causes, the policy view, the model-prose renderer with its check, the held-out pack, the independent review and the registration amendment.
4. **Slice 4: scale and reach.** The large world, the training export, media and other languages, and compound threads.

## Risks / Trade-offs

- **The catalogue encodes the home system's view of knowledge.** Four in five entries are home-defined, and twelve families mirror the product's own dimensions. The lineage marks, the independent review and the per-group report show the risk and do not remove it.
- **The oracle can be wrong.** A wrong mechanism or norm yields confident wrong keys at scale. The mutation and invariance checks catch a key that ignores its plant. The full-history arm catches a key that a careful reader of every session rejects. Neither catches an error that the oracle and that reader share.
- **Most computed keys rest on a background norm.** 357 of 473 do. A norm that readers do not share turns a computed key into an opinion. The per-norm report is the instrument for that, and it needs development rounds to say anything.
- **The recipe form may be too weak.** 46 recipes fit it with no new mechanism or operation. That is one recipe in ten, none has been planted, and 26 of them belong to slice 1. Decision 2 names the result that would end the design, and the first family of each later slice repeats the reading.
- **Templates can be pattern-matched.** A product could learn the surface banks. Disjoint banks per split, a held-out pack, paraphrased probes and the optional model prose reduce it.
- **The world can feel synthetic.** A tidy world understates difficulty. The profile and the packs carry the realism, and the first rounds will show where they fall short.
- **Small rounds say little per family.** A 12-session project gives pooled readings only. Per-family readings need deeper rounds, which cost model calls.
- **The generated family may never earn a decision.** If the concordance check fails and the cause is not found, generated worlds stay a development instrument. That is still the first slice's purpose.
- **Predictions can anchor the reader.** A filed prediction of failure may excuse a failure. The report therefore prints the position between floor and ceiling first.

## Open Questions

The owner answered on 2026-11-10:

1. **Standing.** Decision 9 stands, kept lean. The review, the concordance round and the amendment are one-time steps before generated worlds may decide, not gates on every run. The review is the adversarial review that every change gets, with the fairness brief. The concordance check is one development round. The amendment is a short registration in the existing protocol.
2. **Seed custody.** Evaluator custody outside the repository, as f38 uses.
3. **Audience.** Build the benchmark so that others could run it: product-neutral, self-contained and documented from the first slice, with the neutral export no later than slice 2. Whether to publish is decided after the first results and an outside fairness audit.

Still open, each decided before the slice that needs it:

4. Which budget covers the first outcome round: the capped low-cost actor, subscription turns or API credits? The round states its cost first. Task 5.1 waits for this answer.
5. May the model-prose renderer run in slice 3, given its cost, or should surface banks grow first?
6. Where do the large-world profile's shape numbers come from? The options are a private file, computed from a real vault and read at run time, numbers committed in the repository, or invented distributions. Decided before slice 4.
