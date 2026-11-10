# synthetic-epistemic-world (delta)

## Purpose

Generate synthetic worlds whose epistemic truth is known by construction, from a catalogue of phenomena held as registry data, and measure memory systems against them with a floor and a ceiling for every family.

## ADDED Requirements

### Requirement: Truth is built before text

The generator SHALL build a world in three layers before it writes any document: the world's history, the statements that holders made about it, and the state that those statements warrant at a date. The oracle SHALL derive every key from the warranted state. No key, accepted form, expected output or example SHALL come from the output of a measured system or of a model. The generator and the oracle SHALL import nothing from the product under test.

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

### Requirement: The phenomenon catalogue is registry data

Every phenomenon, family, behaviour, domain pack, surface form and bridge SHALL be an entry in a YAML registry. The truth mechanisms, key classes, probe forms, implicitness levels, stages, scorers and dials SHALL be closed sets that one schema file declares and code implements. Code SHALL branch only on those closed values and SHALL NOT branch on a phenomenon id, a family key or a behaviour key. The loader SHALL refuse an entry that names an undeclared value or an unknown reference, and the refusal SHALL name the entry and the value.

#### Scenario: A new phenomenon reuses existing mechanisms

- **WHEN** a contributor adds a phenomenon whose truth mechanism and probe forms are already declared
- **THEN** the change touches registry files only
- **AND** the generator can plant the phenomenon without a code change once its recipe exists

#### Scenario: An entry names an undeclared mechanism

- **WHEN** a phenomenon names a truth mechanism that the schema file does not declare
- **THEN** the loader refuses the catalogue
- **AND** the refusal names the phenomenon and the undeclared mechanism

### Requirement: Each phenomenon declares how its truth is known

Each phenomenon SHALL declare one key class: `deterministic-oracle`, `rubric-track` or `out-of-scope`. A `deterministic-oracle` phenomenon SHALL name the truth mechanisms that compute its key. A `rubric-track` phenomenon SHALL be scored by a blind judge against a rubric that is fixed before the run. An `out-of-scope` phenomenon SHALL generate no probe. The generator SHALL refuse to plant a phenomenon whose stage is not built, and the refusal SHALL name it.

#### Scenario: A round asks for a phenomenon that is not built

- **WHEN** a round's plan names a phenomenon whose stage is later than the generator's built stage
- **THEN** generation stops with an error that names the phenomenon and its stage

#### Scenario: A probe's key is not unique

- **WHEN** the oracle finds two warranted answers to one probe under its mechanism
- **THEN** generation refuses the probe and names the thread

### Requirement: The oracle is checked without a model

The benchmark SHALL prove each built truth mechanism with three model-free checks. Removing a thread's planted change SHALL move the thread's key to its stable value. Adding an unrelated statement, or renaming every entity consistently, SHALL leave every key the same up to that renaming. Withdrawing one of two independent supports SHALL leave the supported conclusion standing.

#### Scenario: A key ignores its planted change

- **WHEN** the mutation check removes a thread's planted change and the thread's key stays the same
- **THEN** the check fails and names the phenomenon

### Requirement: One planted change decides each probe

The planner SHALL plant each phenomenon as a thread on the entities of one shared world, and SHALL reserve every attribute that the thread's key reads. No other thread SHALL change a reserved attribute, except a compound thread that declares both phenomena. A probe's task and its session's files SHALL NOT contain its key, an accepted form or a trap value. Each probe SHALL carry its proof trace: the artefacts that hold its answer.

#### Scenario: Two threads need the same attribute

- **WHEN** the planner draws a second thread whose recipe would change a reserved attribute
- **THEN** the planner places the thread on other entities or drops it
- **AND** the manifest records the drop

#### Scenario: A probe's session holds its answer

- **WHEN** a session's files contain a probe's key value
- **THEN** the case check refuses the project before any session runs

### Requirement: The same inputs give the same world

Generation SHALL be deterministic: one seed, one catalogue digest, one set of pack digests and one generator version SHALL give byte-identical output. A manifest SHALL pin those four identities and the hash of every artefact. Generated worlds SHALL NOT be committed to the repository.

#### Scenario: A world is generated twice

- **WHEN** the generator runs twice with the same seed, catalogue, packs and version
- **THEN** the two manifests are byte-identical

### Requirement: Splits are separate worlds

The benchmark SHALL keep three splits: `train`, `dev` and `heldout`. A world SHALL belong to one split, and its split SHALL be part of its seed derivation and of every identifier in it. The splits SHALL share no entity name and no surface form. A loader SHALL refuse to mix splits. The training export SHALL refuse a world that is not `train`. A round SHALL refuse a sensor whose training manifest lists a `dev` or `heldout` digest. A held-out seed SHALL stay in evaluator custody, and the repository SHALL hold only a digest that commits to it. A held-out world SHALL NOT appear in a prompt or a fixture, and SHALL back one decision. No probe of it SHALL be repaired or dropped after its run.

#### Scenario: A training export is asked for a development world

- **WHEN** the training export receives a world whose split is `dev`
- **THEN** it refuses and names the world and its split

#### Scenario: A held-out world is used a second time

- **WHEN** a decision run names a held-out world that an earlier decision already read
- **THEN** the run refuses and names the earlier decision

#### Scenario: A surface form is checked across splits

- **WHEN** the split check finds one surface form or one entity name in two splits
- **THEN** the check fails and names it

### Requirement: The world carries no product's vocabulary

The world, its statements and its rendered text SHALL carry no product's ontology. One projection per product SHALL map the world to that product, and it SHALL be the only place that names the product. Each projection SHALL write a parity report that marks every fact as represented, degraded or unsupported. The neutral artefacts, parsed by a product's own grammar, SHALL yield no typed unit and no typed relation. Every arm SHALL receive the same sessions and files.

#### Scenario: Neutral text parses as product structure

- **WHEN** the fairness gate parses a neutral artefact with a product's grammar and finds a typed unit
- **THEN** the gate fails and names the artefact and the surface form that produced it

#### Scenario: A projection cannot express a fact

- **WHEN** a product has no form for a fact of the warranted state
- **THEN** the projection's parity report marks the fact unsupported with a reason
- **AND** the fact is not dropped silently

### Requirement: Rendering changes the text and never the key

Each statement's content SHALL be fixed before rendering. A renderer SHALL choose an implicitness level from the declared set and a registered surface form, and SHALL record both in the manifest. Only a bridge declared as entailing SHALL yield a computed key for an implied statement. A probe SHALL NOT share a surface form with the artefact that answers it. An artefact rendered below the `stated` level SHALL NOT contain the explicit form of its statement.

#### Scenario: The same thread is rendered at two levels

- **WHEN** one thread is rendered at the `stated` level and again at the `implied` level
- **THEN** both renderings carry the same key and the same proof trace

#### Scenario: The ceiling arm cannot read a bridge

- **WHEN** the gold-evidence arm fails a bridge's probes on the development split beyond the declared share
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

### Requirement: Every family is read between a floor and a ceiling

Each outcome round SHALL play a no-memory arm and a gold-evidence arm beside the memory arms, on the same probes and in the same time window. A report SHALL give each arm's position between those two arms for each family, with the paired differences and their intervals. A family whose floor and ceiling are too close SHALL read `no room`. A family in which the primary arm reaches the ceiling on two rounds in a row SHALL read `saturated`. The next round SHALL then raise one of that family's declared dials. A dial SHALL be declared before a round. A report SHALL give no aggregate score, and a family that a round did not sample SHALL read `not measured`.

#### Scenario: The primary arm matches the ceiling

- **WHEN** the primary arm is within the declared distance of the gold-evidence arm in a family on two rounds in a row
- **THEN** the report marks the family saturated
- **AND** the next round's plan raises a dial for that family

#### Scenario: The floor arm already solves a family

- **WHEN** the lower bound of the ceiling's lead over the floor is under the declared threshold
- **THEN** the family reads `no room`
- **AND** no arm's position is reported for it

#### Scenario: A round skips a family

- **WHEN** a round's plan samples no probe from a family
- **THEN** the report shows that family as `not measured`

### Requirement: Expected failures are filed before a run

Each product's projection SHALL state, for each family, whether the product models it and which outcome is expected: pass, partial or fail. A run's manifest SHALL pin the digest of that file as it stood before the run. The report SHALL print each prediction beside the observed result and SHALL list every family whose result contradicts its prediction.

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

A generated world SHALL serve development rounds without registration. Before a decision reads a generated world, an independent lane SHALL review the generator, the catalogue and the projections. That lane SHALL have written none of them, and its brief SHALL be to find every way they disadvantage another arm or product. A concordance check SHALL compare a generated project with a hand-written one in the same window. A preregistration amendment SHALL register the family with its decision rules. The repository SHALL publish no table that compares products.

#### Scenario: A decision run names an unregistered generated family

- **WHEN** a held-out run names a generated world and no acknowledged amendment registers its family
- **THEN** the run refuses

#### Scenario: A generated project and a hand-written one disagree

- **WHEN** the concordance check finds that the two projects rank the arms differently in a dimension they share
- **THEN** the report marks generated results in that dimension as suspect until the cause is recorded

### Requirement: The first projections feed the existing instruments

The generator SHALL write a project in the epistemic task suite's case format, with its split file and digests, so that the suite's runner and case check read it unchanged. The generator SHALL write a product vault seed through the product's own tools, with the list of display marks that each unit should carry. A model-free check SHALL read the marks back from the product and compare them with that list.

#### Scenario: The task suite loads a generated project

- **WHEN** the epistemic task suite loads a generated development project
- **THEN** its case check passes
- **AND** each probe names its planted change and its label

#### Scenario: A seeded unit carries the wrong mark

- **WHEN** the mark check reads a unit whose mark differs from the projection's list
- **THEN** the check fails and names the unit, the expected mark and the mark it read

### Requirement: Model usage is stated and bounded

Generation, the oracle's checks, the template renderer and the projections SHALL use no model. An outcome round SHALL state its expected cost and the question it answers before it starts. A metered round SHALL carry a hard cap, and unknown charges SHALL stay reserved. The reference arms SHALL run probe calls only.

#### Scenario: A metered round would exceed its cap

- **WHEN** the next call's reservation would pass the round's cap
- **THEN** the round stops and reports the work it did not run
