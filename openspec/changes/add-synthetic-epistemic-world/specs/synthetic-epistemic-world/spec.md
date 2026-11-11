# synthetic-epistemic-world (delta)

## Purpose

Generate synthetic worlds whose epistemic truth is known by construction, from a catalogue of phenomena held as registry data, and measure memory systems against them with a floor and a ceiling for every family.

## ADDED Requirements

### Requirement: Truth is built before text

The generator SHALL build a world in three layers before it writes any document: the world's history, the statements that holders made about it, and the state that those statements warrant at a date. The oracle SHALL derive every key from the warranted state. No key, accepted form, expected output or example SHALL come from the output of a measured system or of a model. The world generator and its oracle SHALL import nothing from the product under test.

#### Scenario: The world changed and no statement recorded it

- **WHEN** the world's history changes a value and no statement up to the probe's date records the change
- **THEN** the key is the last recorded value with its date
- **AND** the key is not the hidden new value

#### Scenario: The evidence supports no answer

- **WHEN** no statement up to the probe's date bears on the question
- **THEN** the key is an abstention, although the world's history holds a value

#### Scenario: A module of the generator imports the product

- **WHEN** any module of the world generator or its oracle imports the product's package
- **THEN** the import-boundary test fails and names the module

### Requirement: A key rests only on registered norms

The oracle SHALL apply only the norms that the norm registry lists. Each norm SHALL declare its basis: `stated` or `background`. A key that rests on a stated norm SHALL have, in its proof trace, the document of the world that states the norm. Which record governs a fact, every causal rule, every chance and every claim that a list is complete SHALL be stated norms. The oracle SHALL NOT read the generator's own model of events or its probabilities. A report SHALL give, for each background norm, the number of keys that rest on it and the share that the full-history arm reaches.

#### Scenario: Two holders disagree and no document says who governs

- **WHEN** two holders assert different values of one attribute and no document of the world names the record that governs it
- **THEN** the key reports the disagreement as open
- **AND** the key prefers neither value

#### Scenario: A key needs a causal rule

- **WHEN** a probe asks what would have followed if an event had been different
- **THEN** the key applies a causal rule that a document of the world states
- **AND** the proof trace holds that document

#### Scenario: Careful readers do not share a background norm

- **WHEN** the full-history arm reaches the keys that rest on one background norm less often than the declared share
- **THEN** the report names the norm
- **AND** its entries move to the rubric track before any held-out world is generated

### Requirement: The phenomenon catalogue is registry data

Every phenomenon, family, behaviour, norm, statement kind, hedge, recipe, domain pack, surface form and bridge SHALL be an entry in a YAML registry. One schema file SHALL declare the closed sets that code implements: the truth mechanisms and their readings, the world operations, the rule shapes, the role kinds, the key parts, the key classes, the probe forms, the statement bases, the commitments, the defeat modes, the implicitness levels, the volatility classes, the scorers and the dials. Code SHALL branch only on those closed values. Code SHALL NOT branch on a phenomenon id, a family key, a behaviour key or a norm key. The loader SHALL refuse an entry that names an undeclared value or an unknown reference, and the refusal SHALL name the entry and the value.

#### Scenario: A new phenomenon reuses existing closed values

- **WHEN** a contributor adds a phenomenon and its recipe, and both name only declared closed values
- **THEN** the change touches registry files only
- **AND** the generator can plant the phenomenon without a code change

#### Scenario: An entry names an undeclared mechanism

- **WHEN** a phenomenon names a truth mechanism that the schema file does not declare
- **THEN** the loader refuses the catalogue
- **AND** the refusal names the phenomenon and the undeclared mechanism

### Requirement: A recipe plants a phenomenon as data

A recipe SHALL name its roles, an order of time slots, a list of world operations over those roles and, for each probe form it keys, the readings that give the key. A recipe SHALL hold no condition, no loop and no expression. A phenomenon that names several truth mechanisms SHALL list them as a set, and the schema file SHALL fix the order in which they apply. Each key part SHALL be one reading of a mechanism that the phenomenon names, or one role taken as it is. A recipe SHALL key every probe form that its phenomenon declares, and no other. A recipe step SHALL be able to apply in the planted world only or in the mutation world only.

#### Scenario: A recipe names an operation that is not declared

- **WHEN** a recipe step names an operation or an argument that the schema file does not declare
- **THEN** the loader refuses the recipe and names the step

#### Scenario: A key reads a mechanism that the phenomenon does not name

- **WHEN** a recipe's key names a reading of a mechanism that its phenomenon does not list
- **THEN** the loader refuses the recipe and names the reading

#### Scenario: A recipe leaves a declared probe form unkeyed

- **WHEN** a phenomenon declares a probe form and its recipe holds no key for that form
- **THEN** the loader refuses the recipe and names the phenomenon and the form

#### Scenario: Two mechanisms decide one key

- **WHEN** a later statement uses an entity's second name and the phenomenon lists the identity and the state mechanisms
- **THEN** the identity mechanism resolves the name before the state mechanism folds the statements
- **AND** the key is the one reading that the recipe names

### Requirement: Each phenomenon declares how its truth is known

Each phenomenon SHALL declare one key class: `deterministic-oracle`, `rubric-track`, `run-relative` or `out-of-scope`. A `deterministic-oracle` phenomenon SHALL name the truth mechanisms that compute its key. A `rubric-track` phenomenon SHALL be scored by a blind judge against a rubric that is fixed before the run. A `run-relative` phenomenon SHALL be keyed from the run's own record and SHALL be reported as harm or description only. An `out-of-scope` phenomenon SHALL generate no probe. The loader SHALL compute which phenomena are buildable: a phenomenon is buildable when it has a recipe and the generator implements every closed value that the phenomenon and the recipe name. The generator SHALL refuse to plant a phenomenon that is not buildable, and the refusal SHALL name what is missing. A slice label SHALL NOT decide whether a phenomenon is planted.

#### Scenario: A round asks for a phenomenon that is not buildable

- **WHEN** a round's plan names a phenomenon whose recipe names a probe form that the generator does not implement
- **THEN** generation stops with an error that names the phenomenon and the probe form

#### Scenario: A probe's key is not unique

- **WHEN** the oracle finds two warranted answers to one probe under its mechanisms
- **THEN** generation refuses the probe and names the thread

### Requirement: The oracle is checked without a model

The benchmark SHALL prove each built truth mechanism with three model-free checks. A probe SHALL depend on its plant: its value SHALL differ between the planted world and the mutation world, or it SHALL have a trap in the planted world and none in the mutation world. A recall control SHALL keep its key in both worlds of every other thread. Adding an unrelated statement, or renaming every entity consistently, SHALL leave every key the same up to that renaming. For the warrant mechanism, withdrawing one of two independent supports SHALL leave the supported conclusion standing. The state mechanism SHALL emit membench status spans and SHALL resolve them with membench's own resolution, and membench's span lint SHALL pass on them.

#### Scenario: A key ignores its planted change

- **WHEN** a probe has the same value and the same traps in the planted world and in the mutation world
- **THEN** the mutation check fails and names the phenomenon

#### Scenario: An abstention probe is checked

- **WHEN** an abstention probe's mutation world holds the statement that answers it
- **THEN** the probe's value differs between the two worlds
- **AND** the mutation check passes

### Requirement: One planted change decides each probe

The planner SHALL plant each phenomenon as a thread on the entities of one shared world, and SHALL reserve every attribute that the thread's key reads. No other thread SHALL change a reserved attribute, except a compound thread that declares both phenomena. A probe's task and its session's files SHALL NOT contain its key value, an accepted form or a trap. A premise probe's task SHALL contain its premise, and the key SHALL hold that premise apart from its traps. Each probe SHALL carry its proof trace and its contest set.

#### Scenario: Two threads need the same attribute

- **WHEN** the planner draws a second thread whose recipe would change a reserved attribute
- **THEN** the planner places the thread on other entities or drops it
- **AND** the manifest records the drop

#### Scenario: A probe's session holds its answer

- **WHEN** a session's files contain a probe's key value
- **THEN** the case check refuses the project before any session runs

#### Scenario: A premise probe presupposes a replaced value

- **WHEN** a premise probe's task presupposes a value that a later decision replaced
- **THEN** the key holds the replaced value as its premise and the current value as its value
- **AND** the case check accepts the task

### Requirement: The same inputs give the same world

The model-free pipeline SHALL be deterministic: one seed, one catalogue digest, one set of pack digests and one generator version SHALL give byte-identical output. A manifest SHALL pin those four identities, the projection digest and the hash of every artefact. A world that uses model-written prose SHALL store that prose, and a later run SHALL read the stored prose and SHALL NOT write it again. Generated worlds SHALL NOT be committed to the repository.

#### Scenario: A world is generated twice

- **WHEN** the generator runs twice with the same seed, catalogue, packs and version, and with model prose off
- **THEN** the two manifests are byte-identical

#### Scenario: A prediction is edited

- **WHEN** a product's projection changes and the catalogue does not
- **THEN** the catalogue digest stays the same
- **AND** a world generated before the edit is still valid

### Requirement: Splits are separate worlds

The benchmark SHALL keep three splits: `train`, `dev` and `heldout`. A world SHALL belong to one split, and its split SHALL be part of its seed derivation and of every identifier in it. The splits SHALL share no entity name, no surface form, no bridge and no work-task template. Before the first decision, a held-out world SHALL use a domain pack that no `train` or `dev` world uses. A loader SHALL refuse to mix splits. The training export SHALL refuse a world that is not `train`. A round SHALL refuse a sensor whose training manifest lists a `dev` or `heldout` digest. A component trained on the training export SHALL NOT be decided on a generated world. A held-out seed SHALL stay in evaluator custody, and the repository SHALL hold only a digest that commits to it. A held-out world SHALL NOT appear in a prompt or a fixture, and SHALL back one decision. No probe of it SHALL be repaired or dropped after its run.

#### Scenario: A training export is asked for a development world

- **WHEN** the training export receives a world whose split is `dev`
- **THEN** it refuses and names the world and its split

#### Scenario: A held-out world is used a second time

- **WHEN** a decision run names a held-out world that an earlier decision already read
- **THEN** the run refuses and names the earlier decision

#### Scenario: A bridge is checked across splits

- **WHEN** the split check finds one surface form, bridge, work-task template or entity name in two splits
- **THEN** the check fails and names it

#### Scenario: A decision names a component that learned from generated worlds

- **WHEN** a decision run on a generated world names a component whose training manifest lists a generated world
- **THEN** the run refuses and names the component

### Requirement: The world's text carries no product's vocabulary

The world, its statements and its rendered text SHALL carry no product's ontology. One projection per product SHALL map the world to that product. Each projection SHALL write a parity report that marks every fact as represented, degraded or unsupported. The neutral artefacts, parsed by a product's own grammar, SHALL yield no typed unit and no typed relation. The banned-vocabulary lint SHALL run over every artefact and every probe. A report generated from the product's vocabulary pack SHALL count the artefacts and probes that hold each shipped label and key. Every arm SHALL receive the same sessions and files. When a round seeds its stores, the plain-folder arm SHALL receive the neutral artefacts as a plain folder of files. A report SHALL state the share of its probes that come from families that mirror the product's own dimensions, and the share that come from entries with home-design lineage.

#### Scenario: Neutral text parses as product structure

- **WHEN** the fairness gate parses a neutral artefact with a product's grammar and finds a typed unit
- **THEN** the gate fails and names the artefact and the surface form that produced it

#### Scenario: An artefact holds a product's tool name

- **WHEN** the banned-vocabulary lint finds a term that only one product uses in an artefact or a probe
- **THEN** generation refuses the world and names the artefact and the term

#### Scenario: A projection cannot express a fact

- **WHEN** a product has no form for a fact of the warranted state
- **THEN** the projection's parity report marks the fact unsupported with a reason
- **AND** the fact is not dropped silently

#### Scenario: A round plays mostly home-shaped families

- **WHEN** most probes of a round come from families that mirror the product's dimensions
- **THEN** the report states that share beside its results

### Requirement: Rendering changes the text and never the key

Each statement's content SHALL be fixed before rendering. A renderer SHALL choose an implicitness level from the declared set and a registered surface form, and SHALL record both in the manifest. Only a bridge declared as entailing SHALL yield a computed key for an implied statement, and that bridge SHALL name the stated rule or the registered background norm that makes it entail. A computed entry at the `silent` level SHALL name a stated norm. A probe SHALL NOT share a surface form with the artefact that answers it. An artefact rendered below the `stated` level SHALL NOT contain the explicit form of its statement.

#### Scenario: The same thread is rendered at two levels

- **WHEN** one thread is rendered at the `stated` level and again at the `implied` level
- **THEN** both renderings carry the same key and the same proof trace

#### Scenario: An absence is keyed without a stated rule

- **WHEN** a computed entry at the `silent` level names no stated norm
- **THEN** the loader refuses the entry and names it

#### Scenario: The full-history arm cannot read a bridge

- **WHEN** the full-history arm fails a bridge's probes on development worlds beyond the declared share
- **THEN** the bridge is removed from its pack before any held-out world is generated

### Requirement: Model-written prose is optional and checked both ways

The template renderer SHALL be the default and SHALL use no model. A model-prose renderer SHALL be off by default. When it is on, each rewritten artefact SHALL pass an anchor check and a round-trip check before it replaces its template rendering. The round-trip check SHALL reject an artefact that lacks a planted statement, and SHALL reject an artefact that holds a statement its manifest lacks. An artefact that fails after the declared retries SHALL keep its template rendering, and the manifest SHALL record that. The check SHALL itself be measured by planted mutations before any round uses it.

#### Scenario: A rewritten note adds a fact

- **WHEN** the round-trip check extracts a statement that the artefact's manifest does not list
- **THEN** the rewrite is rejected
- **AND** the artefact keeps its template rendering after the declared retries

#### Scenario: The check misses planted mutations

- **WHEN** the mutation measurement finds that the check catches fewer planted drops and additions than the declared share
- **THEN** the model-prose renderer stays off for scored rounds

### Requirement: The full-history arm gates the keys on development worlds

On a development world, an arm with no memory and the full text of every earlier session SHALL solve at least the declared share of the probes. Each probe that it fails SHALL be repaired or dropped, and the report SHALL record the repair rate. An arm with no memory and no history SHALL fail at least the declared share of the probes that are read as a position, and each such probe that it passes SHALL be repaired or dropped. That second gate SHALL NOT apply to a probe that is read as harm. The gold-evidence arm SHALL NOT be the check on a key.

#### Scenario: A key ignores a contrary statement

- **WHEN** the oracle's key ignores a statement in an earlier session and the full-history arm answers otherwise
- **THEN** the probe is flagged, and it is repaired or dropped before any held-out world is generated

#### Scenario: The no-memory arm abstains correctly

- **WHEN** the no-memory arm answers an abstention probe correctly
- **THEN** the memory-needed gate does not flag the probe

### Requirement: Every family is read between a floor and a ceiling

Each outcome round SHALL play a no-memory arm and a gold-evidence arm beside the memory arms, on the same probes and in the same time window. The gold-evidence arm SHALL receive each probe's contest set in session order: the proof trace, and every artefact that holds one of the probe's traps or a statement that the key's norms defeat. Each probe form SHALL declare whether it is read as a position or as harm. A report SHALL give each arm's position between the floor and the ceiling for a family, with the paired differences and their intervals. It SHALL give harm as the share of probes that the no-memory arm gets right and the arm gets wrong. It SHALL give each family's false-alarm rate on its mutation-world and twin probes beside its position. A report SHALL print a family's position only when the window holds at least the declared number of position probes for it and the lower bound of the ceiling's lead over the floor reaches the declared threshold. A family SHALL read `saturated` only when the lower bound of the interval between the primary arm and the ceiling stays above the declared margin on two reports in a row. A round plan SHALL declare each dial's value for each family before the round runs, including the share of threads planted in the mutation world. No score SHALL span rounds or the catalogue, and a family that a round did not sample SHALL read `not measured`.

#### Scenario: A family has three probes

- **WHEN** a window holds fewer position probes for a family than the declared number
- **THEN** the family reads `too few probes` with its raw counts
- **AND** no position, `no room` or `saturated` reading is printed for it

#### Scenario: The floor arm already solves a family

- **WHEN** the upper bound of the ceiling's lead over the floor is under the declared threshold
- **THEN** the family reads `no room`
- **AND** no arm's position is reported for it

#### Scenario: The primary arm matches the ceiling

- **WHEN** the lower bound of the interval between the primary arm and the gold-evidence arm is above the declared margin on two reports in a row
- **THEN** the report marks the family saturated
- **AND** the next round's plan raises a dial for that family

#### Scenario: An arm flags everything

- **WHEN** an arm reports a change on most mutation-world probes of a family
- **THEN** the report shows that family's false-alarm rate beside its position

#### Scenario: The ceiling arm must resolve a contest

- **WHEN** a probe's trap is a replaced value that an earlier artefact holds
- **THEN** the gold-evidence arm's prompt holds that earlier artefact beside the proof trace

#### Scenario: A round skips a family

- **WHEN** a round's plan samples no probe from a family
- **THEN** the report shows that family as `not measured`

### Requirement: Expected failures are filed before a run

Each product's projection SHALL state, for each family, whether the product models it and which outcome is expected: pass, partial or fail. The projection SHALL sit outside the catalogue. A run's manifest SHALL pin the digest of that file as it stood before the run. The report SHALL print each prediction beside the observed result and SHALL list every family whose result contradicts its prediction.

#### Scenario: A family passes against its prediction

- **WHEN** a family predicted to fail reads at its ceiling
- **THEN** the report lists the family as a contradicted prediction
- **AND** the entry names the probes to read before the result is trusted

### Requirement: Arms are compared case by case, and a blind judge decides free text

A round SHALL compare arms on the same probes and SHALL report each paired difference with its confidence interval. A blind judge SHALL decide the correctness of a free-text answer from the task, the key and the answer, and SHALL NOT see the arm. Code SHALL score a structured answer field: an exact value, a set, an order or a probability. Failures of a product SHALL stay in the denominator, and a fault of the harness SHALL invalidate the window for every arm.

#### Scenario: A probe asks for an order

- **WHEN** a probe asks for events in order through a structured field
- **THEN** code scores the answer by rank correlation with the key
- **AND** the judge does not score it

### Requirement: Generated worlds decide only after independent review and registration

A generated world SHALL serve development rounds without registration. Before a decision reads a generated world, an independent lane SHALL review the generator, the catalogue and the projections. That lane SHALL have written none of them, and its brief SHALL be to find every way they disadvantage another arm or product. A concordance check SHALL compare a generated project with a hand-written one in the same window, on the paired difference between the primary arm and the plain-folder arm over all change probes of each project. A preregistration amendment SHALL register the family with its decision rules. The repository SHALL publish no table that compares products.

#### Scenario: A decision run names an unregistered generated family

- **WHEN** a held-out run names a generated world and no acknowledged amendment registers its family
- **THEN** the run refuses

#### Scenario: A generated project and a hand-written one disagree

- **WHEN** the intervals of the pooled difference on the two projects do not overlap
- **THEN** the report marks generated results as suspect until the cause is recorded

### Requirement: Projections feed the existing instruments

The generator SHALL write a project in the epistemic task suite's case format, with its split file and digests, so that the suite's runner and case check read it. The generator SHALL write store seeds in three forms: a plain folder of the neutral artefacts, a product vault with typed structure and a product vault without it. Code that writes into a product SHALL live outside the world generator's package. The product's projection SHALL list the display marks that each seeded unit should carry, and a model-free check SHALL read the marks back from the product and compare them with that list.

#### Scenario: The task suite loads a generated project

- **WHEN** the epistemic task suite loads a generated development project
- **THEN** its case check passes
- **AND** each probe names its planted change and its label

#### Scenario: A seeded unit carries the wrong mark

- **WHEN** the mark check reads a unit whose mark differs from the projection's list
- **THEN** the check fails and names the unit, the expected mark and the mark it read

### Requirement: Model usage is stated and bounded

Generation, the oracle's checks, the template renderer and the projections SHALL use no model. An outcome round SHALL state its expected cost and the question it answers before it starts. A metered round SHALL carry a hard cap, and unknown charges SHALL stay reserved. The gold-evidence arm and the full-history arm SHALL run probe calls only.

#### Scenario: A metered round would exceed its cap

- **WHEN** the next call's reservation would pass the round's cap
- **THEN** the round stops and reports the work it did not run
