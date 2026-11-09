## 1. Specification

- [ ] 1.1 Write the proposal, the design and the deltas. Validate strictly with the CI-pinned OpenSpec.
- [x] 1.2 Record the owner's choice for degree of belief (levels plus sourced numbers, 2026-11-08). Then add the MODIFIED deltas for the bootstrap commitments and the verdict remediation text.
- [ ] 1.3 Agree the split with the neighbours: `relates_to` retyping, the single-store edge table, the sensed epistemic model and approved procedures.

## 2. Relation behaviours

- [x] 2.1 Record the inventory's mapping in design.md: the relation-behaviour table, the `frontmatter_fields` binding and the debt list.
- [x] 2.2 Declare the closed relation-behaviour set and the `behaviours` field on core entries. Resolve an extension's behaviours through its core parent in one registry accessor. Refuse `behaviours` on an extension.
- [x] 2.3 Add `frontmatter_fields` to the core pack, and derive the `frontmatter` origin from it. Make graph indexing, relation facts and activation read it, and delete the three copies.
- [x] 2.4 Replace each relation branch that the design table maps with a behaviour or binding read. SQL readers ask `exomem_edge_behaves`, or expand keys over the admitted instances through `relation_query_plan`. Give each remaining branch its debt reason in a code comment.
- [x] 2.5 Move the shipped traversal profiles into the core pack, in the vault overlay's grammar.
- [x] 2.6 Prove the cutover on the fixture vault in design.md ("Fixture"): core outputs identical, and each intended change in design.md shown by its own case.
- [x] 2.7 Update the AGENTS.md section "Vocabulary lives in registries": code branches on `behaviours`, `traits`, `role` and status `class`, never on a family value.
- [ ] 2.8 Add `precedes` and `follows` with their opposite order behaviours, and order them in compiled context. The relations and their `earlier_than_target` and `later_than_target` behaviours are in the core pack. The ordering lands with `order_in_time` in slice 5.

## 3. Kinds and verdicts

- [ ] 3.1 Record the kind mapping in design.md: the kind-trait table and the kind debt.
- [ ] 3.2 Move the 27 built-in kinds, their aliases and the compact `observation` kind into the shipped pack. Declare the traits `assertion`, `page_claim`, `procedure`, `outcome` and `question`. Replace the kind reads with trait reads, and prove identical output on the fixture vault in design.md ("Fixture").
- [ ] 3.3 Move the verdicts into a registry with labels. Update the verdict remediation, bootstrap's taught verdicts and the experiment `outcome:` check.

## 4. Belief and the new keys

- [ ] 4.1 Add the `belief` kind, the belief-level registry, the `credence`, `credence_holder` and `credence_basis` keys and the `assesses` relation, with validation that names the missing key and points misplaced rows to the belief form.
- [ ] 4.2 Add `asserted_by` and `modality`, with context that presents attributed claims and keeps modalities apart.
- [ ] 4.3 Add `occurred_at`, the three quantity keys and `inference_mode` with its registry.
- [ ] 4.4 Reserve the new keys, with warnings for one release on pages that already use them.

## 5. Dimensions

- [ ] 5.1 Add the dimension registry with the 12 shipped dimensions and their members.
- [ ] 5.2 Add filtering in retrieval, grouping in the context compiler, and queues in reviews and reports, for any dimension.
- [ ] 5.3 Add the closed display-behaviour set, and prove that no display behaviour hides a unit or changes its standing. Declare the `closable` kind trait with `flag_open`, its only reader.
- [ ] 5.4 Check on real models that the display annotations help an agent rather than add noise.

## 6. Language and teaching

- [ ] 6.1 Add labels per language to the kind, verdict, modality, belief-level, inference-mode and dimension registries, and parse labels as aliases.
- [ ] 6.2 Teach the language from the shipped pack in bootstrap and the skill scaffold, keeping the scaffold generic. Rewrite the no-credence teaching in the scaffold's frontmatter, skill and vault-care references, `judge_categorically` and the verdict-set comment, and say that `credence` is a unit row, never frontmatter.

## 7. Proof and delivery

- [ ] 7.1 Prove each slice on real models: agents choose the right word on realistic and hostile inputs.
- [ ] 7.2 Review, merge, release, and archive this change. Before the archive, refresh each MODIFIED block against the canonical spec, keeping the requirements and scenarios that later changes added. Three other changes copy "Current Relation Review Disposition".
