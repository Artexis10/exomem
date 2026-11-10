## Why

Exomem's claim is epistemic. It keeps what is current, what was corrected, where a claim came from and whose it is. It keeps what kind of statement a claim is, what it rests on and where it holds. No benchmark tests that claim across its range.

- **Outside benchmarks cover a few phenomena each.** LongMemEval tests updates and abstention. STALE tests implicit invalidation. MemConflict tests three conflict types. RECON tests cascades and counterfactuals. Each uses its own fixed cases, and most build the text first and the answer key afterwards.
- **The in-repo benches are hand-written.** The epistemic task suite (f38) plays three projects that an independent lane wrote by hand, with 11 dimensions and two probes per dimension. The vault seeds of the display-marks check are hand-written too. Hand-writing does not reach hundreds of phenomena, and it gives no fresh cases after each repair.
- **membench generates cases, as islands.** Its 24 scenario templates each build their own entities and hand-written status spans. No template shares a world with another, no statement is implicit, and no holder has a belief.
- **A home benchmark that the home product aces is misleading.** A score needs a floor and a ceiling beside it, and the benchmark needs phenomena that the product does not model today.

Exomem needs its own benchmark, centred on epistemics, on a synthetic world that feels real and copies nobody's life. Its scope is the whole of how knowledge works, not one short list of cases.

## What Changes

- **A catalogue of epistemic phenomena, as registry data.** This change ships the first catalogue in `catalogue/`: 516 phenomena in 36 families and 9 realms. Each entry names how its truth is known, how it is probed and which slice builds it. Seven entries declare limits that no benchmark can test. Adding a phenomenon is a data change.
- **A closed set of truth mechanisms.** Thirteen pure functions derive every key from the generated world. Examples are a fold over dated statements, a warrant graph, a ledger of holders and a scope lattice. `catalogue/attributes.yaml` lists all of them. Code implements mechanisms. It never branches on a phenomenon or a family.
- **A world generator that puts truth before text.** A seed builds one coherent world: entities, a history of events, and the statements that people and systems made about them. The generator plants each phenomenon as a thread in that world. An oracle derives what the evidence warrants at each date. Renderers write the documents last.
- **Implicit statements with known truth.** A statement's content is fixed before rendering. The renderer then chooses how openly the text carries it, from stated to silent, through registered bridges. Model-written prose is optional and off by default. A two-way check rejects a note that drops or adds a fact.
- **A floor and a ceiling for every family.** Each round plays a no-memory arm and a gold-evidence arm beside the memory arms. A report gives each arm's place between the two. A family at its ceiling is reported as saturated, and the next round raises its difficulty dials.
- **A prediction register.** Before a run, each product's projection states the families where the product is expected to fail. The report prints the prediction beside the result.
- **Splits as separate worlds.** Training, development and held-out worlds share no seed, no name and no surface form. A held-out seed stays in evaluator custody, and one decision uses it once.
- **Projections to the existing instruments.** The first slice writes a generated project in the f38 case format, and an Exomem vault seed with known display marks. Later slices write a membench corpus, a large world for latency and a labelled training export.

## Capabilities

### New Capabilities

- `synthetic-epistemic-world`: the phenomenon catalogue, the world generator, the truth oracle, the renderers, the splits, the projections and the rules for measuring with them.

### Modified Capabilities

None. The f38 requirement text is still in the active change `add-epistemic-task-bench`, so this change adds no delta to it. A generated project is a valid f38 input for development rounds. A later preregistration amendment registers generated worlds as their own family before any decision reads them.

## Impact

- **Benchmarks:** a `world` subpackage of `benchmarks/membench/`, with its catalogue, domain packs and surface banks as YAML. It reuses membench's wordbank, record schema, oracle, native renderers, runner and outcome suites. Nothing ships in the wheel or the sdist. Generated worlds are never committed.
- **Product:** no product change. The generator and the oracle import nothing from `exomem`.
- **Models:** the world, the oracle, the template renderer and the projections use no model. The model-prose renderer is off by default, and a failed note falls back to its template rendering. Actors, the prose writer, the extractor of the fidelity check and the judge run in the benchmark, outside the product, so the pure-substrate boundary is not in play.
- **Cost:** generation and its checks cost nothing. An outcome round costs model usage. The design keeps rounds small: the two reference arms need probe calls only. Each run states its cost first and waits for the owner's agreement when it is a dollar or more.
- **Fairness:** the catalogue, the generator and the projections come from the product's own lanes. An independent lane must attack them before any decision run, and this repository publishes no table that compares products.
- **Old debt, reported and not changed here:** membench's family registry, claim statuses, authority tiers and banned-vocabulary list are code constants, and so is the f38 behaviour list.
