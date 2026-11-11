## Why

Exomem's claim is epistemic. It keeps what is current, what was corrected, where a claim came from and whose it is. It keeps what kind of statement a claim is, what it rests on and where it holds. No benchmark tests that claim across its range.

- **Outside benchmarks cover a few phenomena each.** LongMemEval tests updates and abstention. STALE tests implicit invalidation. MemConflict tests three conflict types. RECON tests cascades and counterfactuals. Each uses its own fixed cases, and most build the text first and the answer key afterwards.
- **The in-repo benches are hand-written.** The epistemic task suite (f38) plays three projects that an independent lane wrote by hand, with 11 dimensions and two probes per dimension. The vault seeds of the display-marks check are hand-written too. Hand-writing does not reach hundreds of phenomena, and it gives no fresh cases after each repair.
- **membench generates cases, as islands.** Its 24 scenario templates each build their own entities and hand-written status spans. No template shares a world with another, no statement is implicit, and no holder has a belief.
- **A home benchmark that the home product aces is misleading.** A score needs a floor and a ceiling beside it, and the benchmark needs phenomena that the product does not model today.

Exomem needs its own benchmark, centred on epistemics, on a synthetic world that feels real and copies nobody's life. Its scope is the whole of how knowledge works, not one short list of cases.

## What Changes

- **A catalogue of epistemic phenomena, as registry data.** This change ships the first catalogue in `benchmarks/membench/world/catalogue/`: 512 phenomena in 36 families and 9 realms. Each entry names how its truth is known, which norms its key rests on, how it is probed and which slice plans to build it. Seven entries declare limits that no benchmark can test. Adding a phenomenon is a data change.
- **A closed set of truth mechanisms.** Thirteen pure functions derive every computed key from the evidence layer of the generated world: the statements that its holders made, and never its hidden events. Examples are a fold over dated statements, a warrant graph, a ledger of holders and a scope lattice. `benchmarks/membench/world/catalogue/attributes.yaml` lists all of them. Code implements mechanisms. It never branches on a phenomenon or a family.
- **Recipes as data.** A recipe plants one phenomenon as a list of closed world operations, and names the mechanism readings that are its keys. It holds no condition and no expression. This change ships 46 recipes: the 26 that slice 1 needs, and 20 for later slices. They cover the nine realms and all thirteen mechanisms.
- **A registry of norms.** Every rule that a key rests on is a registered norm. A norm is either stated in a document of the world or declared as background, and a report shows which keys rest on background norms.
- **A world generator that puts truth before text.** A seed builds one coherent world: entities, a history of events, and the statements that people and systems made about them. The generator plants each phenomenon as a thread in that world. An oracle derives what the evidence warrants at each date. Renderers write the documents last.
- **Implicit statements with known truth.** A statement's content is fixed before rendering. The renderer then chooses how openly the text carries it, from stated to silent, through registered bridges. Model-written prose is optional and off by default. A two-way check rejects a note that drops or adds a fact.
- **A floor and a ceiling for every family.** Each round plays a no-memory arm and a gold-evidence arm beside the memory arms. A report gives each arm's place between the two, and its false-alarm rate on cases where nothing changed. A family with too few probes says so. A family at its ceiling is reported as saturated, and the next round raises its difficulty dials.
- **Keys gated by a full-history reader.** On development worlds, an arm that reads every earlier session must reach the keys. A key, a bridge or a norm that it rejects is repaired or dropped.
- **A prediction register.** Before a run, each product's projection states the families where the product is expected to fail. The report prints the prediction beside the result.
- **Splits as separate worlds.** Training, development and held-out worlds share no seed, name, surface form, bridge or work-task template. A held-out world uses a domain pack of its own. A held-out seed stays in evaluator custody, and one decision uses it once.
- **Projections to the existing instruments.** The first slice writes a generated project in the f38 case format. Later slices write a membench corpus, store seeds with known display marks, a neutral export, a large world for latency and a labelled training export.
- **Four changes to the f38 case format.** The suite reads its behaviours from the registry. A key holds a list of traps, keeps a premise apart from them and names how the probe is read.

## Capabilities

### New Capabilities

- `synthetic-epistemic-world`: the phenomenon catalogue, the world generator, the truth oracle, the renderers, the splits, the projections and the rules for measuring with them.

### Modified Capabilities

- `epistemic-utility-regression`: one added requirement for the epistemic task suite's keys. The suite reads its behaviours from the registry, holds a list of traps, keys a premise apart from the traps and names a probe's reading. The suite's other requirement text is still in the active change `add-epistemic-task-bench`, and this change does not restate it.

A generated project is an f38 input for development rounds only. A later preregistration amendment registers generated worlds as their own family before any decision reads them.

## Impact

- **Benchmarks:** a `world` subpackage of `benchmarks/membench/`, with its catalogue, domain packs and surface banks as YAML. It reuses membench's wordbank, record schema, time resolution, span lint, native renderers, runner and outcome suites. Nothing ships in the wheel or the sdist. Generated worlds are never committed.
- **Product:** no product change. The generator and the oracle import nothing from `exomem`.
- **Models:** the world, the oracle, the template renderer and the projections use no model. The model-prose renderer is off by default, and a failed note falls back to its template rendering. Actors, the prose writer, the extractor of the fidelity check and the judge run in the benchmark, outside the product, so the pure-substrate boundary is not in play.
- **Cost:** generation and its checks cost nothing. An outcome round costs model usage, and a development round costs a full f38 round until the no-memory arm plays probes only. Each run states its cost first and waits for the owner's agreement when it is a dollar or more. Recipes, surface forms and bridges are authored work. design.md gives a drafting rate for recipes from one lane's own session clock, which no run file backs, and no rate yet for surface forms or bridges.
- **Fairness:** the catalogue, the generator and the projections come from the product's own lanes, and twelve of the 36 families mirror the product's shipped dimensions. A report states the share of its probes that come from them. An independent lane must attack the benchmark before any decision run, and this repository publishes no table that compares products.
- **Epistemic task suite:** its judge, case check, gates and report change as the delta says. Its hand-written projects stay valid.
- **Old debt, reported and not changed here:** membench's family registry, claim statuses, authority tiers and banned-vocabulary list are code constants. This change retires one such constant, the f38 behaviour list.
