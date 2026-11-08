## 1. Specification

- [ ] 1.1 Write the proposal, the design and the deltas. Validate strictly with the CI-pinned OpenSpec.
- [x] 1.2 Record the owner's choice for degree of belief (levels plus sourced numbers, 2026-11-08). Then add the MODIFIED deltas for the bootstrap commitments and the verdict remediation text.
- [ ] 1.3 Agree the split with the neighbours: `relates_to` retyping, the single-store edge table, the sensed epistemic model and approved procedures.

## 2. Relation behaviours

- [ ] 2.1 Inventory every branch on a relation key or family value in `src/exomem`. Map each to a behaviour, add a behaviour with its code, or name it as debt.
- [ ] 2.2 Declare the closed relation-behaviour set, give the core relations their behaviours, and make extensions inherit them.
- [ ] 2.3 Replace the hard-coded lift, membership, resolution, prediction-evidence, provenance and supersession sets with behaviour reads. Prove identical output on a fixture vault.
- [ ] 2.4 Add `precedes` and `follows` with their opposite order behaviours, and order them in compiled context.

## 3. Kinds and verdicts

- [ ] 3.1 Inventory every branch on a kind key. Map each to a trait or name it as debt.
- [ ] 3.2 Move the built-in kinds into the shipped pack with their traits. Replace the kind branches with trait reads, and prove identical output on a fixture vault.
- [ ] 3.3 Move the verdicts into a registry with labels. Update the verdict remediation, bootstrap's taught verdicts and the experiment `outcome:` check.

## 4. Belief and the new keys

- [ ] 4.1 Add the `belief` kind, the belief-level registry, the `credence`, `credence_holder` and `credence_basis` keys and the `assesses` relation, with validation that names the missing key and points misplaced rows to the belief form.
- [ ] 4.2 Add `asserted_by` and `modality`, with context that presents attributed claims and keeps modalities apart.
- [ ] 4.3 Add `occurred_at`, the three quantity keys and `inference_mode` with its registry.
- [ ] 4.4 Reserve the new keys, with warnings for one release on pages that already use them.

## 5. Dimensions

- [ ] 5.1 Add the dimension registry with the 12 shipped dimensions and their members.
- [ ] 5.2 Add filtering in retrieval, grouping in the context compiler, and queues in reviews and reports, for any dimension.
- [ ] 5.3 Add the closed display-behaviour set, and prove that no display behaviour hides a unit or changes its standing.
- [ ] 5.4 Check on real models that the display annotations help an agent rather than add noise.

## 6. Language and teaching

- [ ] 6.1 Add labels per language to the kind, verdict, modality, belief-level, inference-mode and dimension registries, and parse labels as aliases.
- [ ] 6.2 Teach the language from the shipped pack in bootstrap and the skill scaffold, keeping the scaffold generic. Rewrite the no-credence teaching in the scaffold's frontmatter, skill and vault-care references, `judge_categorically` and the verdict-set comment, and say that `credence` is a unit row, never frontmatter.

## 7. Proof and delivery

- [ ] 7.1 Prove each slice on real models: agents choose the right word on realistic and hostile inputs.
- [ ] 7.2 Review, merge, release, and archive this change.
