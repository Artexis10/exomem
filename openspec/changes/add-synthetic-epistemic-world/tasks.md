## 1. Specification

- [x] 1.1 Write the proposal, the design, the first catalogue and the delta. Validate strictly with the CI-pinned OpenSpec.
- [ ] 1.2 Agree the open questions in design.md with the owner. Standing, seed custody and audience were answered on 2026-11-10; the first round's budget, model prose and shape numbers are decided before the slice that needs each.
- [ ] 1.3 Agree the seams with the neighbours: `add-epistemic-task-bench` for the project format and the gold-evidence arm, `make-epistemic-language-dynamic` task 5.4 for the vault seeds, and `add-sensed-epistemic-model` for the training export.

## 2. Slice 1: catalogue and world core, with no model

- [ ] 2.1 Move the catalogue to `benchmarks/membench/world/catalogue/` and add its loader. Check: the loader refuses an undeclared value, an unknown reference, a duplicate id and a stage that contradicts its key class, each by name. The loader's family counts equal the table in design.md.
- [ ] 2.2 Build the world core: entities, attributes over time, events, statements, artefacts and the world validator. Check: one failing fixture per invariant. An import-boundary test finds no `exomem` import under `membench/world/`.
- [ ] 2.3 Build seven mechanisms at one hop: `state_fold`, `kind_tag`, `holder_ledger`, `identity_map`, `scope_lattice`, `warrant_graph` and `corpus_closure`. Check, for each: the mutation, invariance and uniqueness checks of design.md pass.
- [ ] 2.4 Build the backbone, three domain packs, the planner with reservations, and the wordbank's split partitions. Check: two runs give identical manifests. No attribute belongs to two threads. The split check finds no shared name or surface form. The privacy gate passes over the packs and over one generated world.
- [ ] 2.5 Write the recipes for the 60 phenomena marked `s1`. Check: each recipe plants in each pack, and each key passes the mutation check.
- [ ] 2.6 Build the template renderer at the levels `stated`, `paraphrased` and `unmarked`, with the anchor check. Check: one thread gives the same key and proof trace at each level. No artefact below `stated` holds an explicit form. No probe shares a surface form with its answering artefact.

## 3. Slice 1: projections

- [ ] 3.1 Write the f38 projection. Check: the suite's existing case check passes on a generated development project from each pack. Each of the 11 dimensions has at least two change probes, and each project has four stable probes. Confirm that the suite accepts a family key as a probe's label, or keep those probes in a separate project file.
- [ ] 3.2 Add the gold-evidence arm to the suite's history modes. Check: the arm's prompt holds the probe's proof-trace artefacts under a neutral header and nothing else from earlier sessions.
- [ ] 3.3 Write the vault seed through the product's tools, with its list of expected marks and a twin vault without typed structure. Check: the marks read back equal the list. The two vaults differ only in typed structure. The parity report lists every fact. The neutral artefacts parse as no typed unit.
- [ ] 3.4 Extend the report with each arm's position between the floor and the ceiling per family, and the readings `no room`, `saturated` and `not measured`. Add each prediction beside its result, and the share of probes per lineage group. Check: fixed score tables give the expected readings, including one contradicted prediction and one family with no sample.

## 4. Slice 1: first rounds

- [ ] 4.1 State the cost and the question. Then run one development world with the arms `N`, `G`, `F` and `P` in one window.
- [ ] 4.2 Read the gates: `G` solves at least 90% of the probes, and `N` fails at least 80%. Repair or drop each recipe and surface form that they flag, and record the repair rate.
- [ ] 4.3 Check each repair on the probes that exposed it. Then judge the next round on a fresh development world.
- [ ] 4.4 Hand the vault seeds to the display-marks check.

## 5. Slice 2: implied change and the model-free layer

- [ ] 5.1 Add bridges and the levels `implied`, `propagated`, `embedded` and `silent`. Check: keys do not change across levels. The gold-evidence arm audits each bridge on the development split, and the manifest records each dropped bridge.
- [ ] 5.2 Add cascades with independent support to `warrant_graph`, and build `interval_order`, `quantity_calc` and `rule_closure`, with the probe forms `impact` and `as_of` and the set and order scorers. Check: the three oracle checks pass for each.
- [ ] 5.3 Write the recipes for the phenomena marked `s2`, one family at a time. Check: as task 2.5.
- [ ] 5.4 Write the membench projection. Check: membench's span lint passes on the derived spans, and the existing runner reads the corpus unchanged.
- [ ] 5.5 Add the dials to the round plan. Check: a plan with an undeclared dial is refused, and the manifest records each dial's value.
- [ ] 5.6 State the cost. Then play the hand-written development project and a generated one in one window, and record where they agree and disagree by dimension.
- [ ] 5.7 Add the neutral export for other products' own harnesses, with its documentation. Check: a world read back from the export holds the same statements and keys, and the export names no Exomem kind, relation or field.

## 6. Slice 3: minds, chance and adversaries

- [ ] 6.1 Build nested `holder_ledger`, `chance_model`, `causal_model` and `policy_view`, with the probe forms `forecast` and `what_if` and the Brier scorer. Check: the three oracle checks pass for each.
- [ ] 6.2 Write the recipes for the phenomena marked `s3`, and the rubric lines for the rubric-track entries. Check: as task 2.5, and a second-family judge re-judges a sample.
- [ ] 6.3 Build the model-prose renderer, off by default, with the anchor and round-trip checks. State the cost first. Check: the mutation measurement reports the share of planted drops and additions that the check catches.
- [ ] 6.4 Commission the independent review of the generator, the catalogue, the surface banks and the projections. Record each finding and its answer.
- [ ] 6.5 File the preregistration amendment that registers the generated family, with its decision rules, thresholds and custody. Record the owner's acknowledgment.
- [ ] 6.6 Tag the generator, generate the first held-out world and commit its commitment digest. State the cost. Then run the decision in two windows.

## 7. Slice 4: scale and reach

- [ ] 7.1 Add the size profile and the large world. Check: the world's page count and shape statistics match the profile, and the latency gate reads it.
- [ ] 7.2 Add the training export. Check: it refuses a world that is not `train`, and a round refuses a sensor trained on a `dev` or `heldout` digest.
- [ ] 7.3 Add the media and language packs. Check: a profile without support reads `unsupported`, never zero.
- [ ] 7.4 Add compound threads. Check: each declares both phenomena, and the mutation check passes for each part.

## 8. Delivery

- [ ] 8.1 After each slice merges, update `benchmarks/README.md` and the benchmark methodology page.
- [ ] 8.2 When the non-optional tasks are done, synchronise the delta into the canonical specs and archive this change with `openspec archive`.
