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

- [x] 3.1 Record the kind mapping in design.md: the kind-trait table and the kind debt.
- [x] 3.2 Move the 27 built-in kinds, their aliases and the compact `observation` kind into the shipped pack. Declare the traits `assertion`, `page_claim`, `procedure`, `outcome` and `question`. Replace the kind reads with trait reads, and prove identical output on the fixture vault in design.md ("Fixture").
- [x] 3.3 Move the verdicts into a registry with labels. Update the verdict remediation, bootstrap's taught verdicts and the experiment `outcome:` check.

## 4. Belief and the new keys

- [x] 4.1 Add the `belief` kind, the belief-level registry, the `credence`, `credence_holder` and `credence_basis` keys and the `assesses` relation, with validation that names the missing key and points misplaced rows to the belief form. Point the `invalid_rich_verdict` remediation to the belief form.
- [x] 4.2 Add `asserted_by` and `modality`, with context that presents attributed claims and keeps modalities apart.
- [x] 4.3 Add `occurred_at`, the three quantity keys and `inference_mode` with its registry.
- [x] 4.4 Reserve the new keys, with warnings for one release on pages that already use them.
- [ ] 4.5 One release after slice 4 ships, turn the first-release warnings into errors. First, extend `semantic_census`, which counts errors only, to list the pages that carry first-release warnings, and repair or report each one. Then set `_FIRST_RELEASE_SEVERITY` to error, and delete `_RESERVED_BEFORE_UNIT_ROW_KEYS` and the placeholder-belief leniency.

## 5. Dimensions

- [x] 5.1 Add the dimension registry with the 12 shipped dimensions and their members.
- [ ] 5.2 Add filtering in retrieval, grouping in the context compiler, and queues in reviews and reports, for any dimension. Slice 5a delivered the `unit.dimension` retrieval filter at unit grain, and the per-dimension unit counts in the category report (`schema_memory` `infer`, subject `categories`). The offline reader evaluates every dimension on every unit. The served reader rewrites a dimension that the index answers, and checks every other dimension on its hits after the limit, so such a search can return fewer hits than its limit; task 5.5 removes that gap. Grouping in the context compiler and the review queues are left for 5b.
- [ ] 5.3 Add the closed display-behaviour set, and prove that no display behaviour hides a unit or changes its standing. Declare the `closable` kind trait with `flag_open`, its only reader. Slice 5a delivered the closed set (`lead_with_current`, `show_origin`, `flag_open`, `order_in_time`) as registry validation only: an undeclared value is refused by name. The effects, the proof that none hides a unit, and `closable` with `flag_open` are left for 5b.
- [ ] 5.4 Check on real models that the display annotations help an agent rather than add noise.
- [ ] 5.5 Answer `unit.dimension` from the index on the managed runtime, so that a filtered search fills its limit. Index each unit's governed row keys as a filter field. Join typed edges when the single-store edge table lands (task 1.3).

## 6. Language and teaching

- [x] 6.1 Add labels per language to the kind, verdict, modality, belief-level, inference-mode and dimension registries, and parse labels as aliases. Verdicts, belief levels, modalities and inference modes took them in slices 3 and 4, dimensions in slice 5a and kinds in slice 6.
- [x] 6.2 Teach the language from the shipped pack in bootstrap and the skill scaffold, keeping the scaffold generic. Rewrite the no-credence teaching in the scaffold's frontmatter, skill and vault-care references, the verdict-set comment and the `observe_memory` verdict docstring, and say that `credence` is a unit row, never frontmatter.
- [ ] 6.3 Make `observe_memory`, the parent index and the lexical store's vault-neutral parse read the page's registry, so that a unit of a vault kind or under a vault label can be addressed and updated.

## 7. Proof and delivery

- [ ] 7.1 Prove each slice on real models: agents choose the right word on realistic and hostile inputs.
- [ ] 7.2 Review, merge, release, and archive this change. Before the archive, refresh each MODIFIED block against the canonical spec, keeping the requirements and scenarios that later changes added. Three other changes copy "Current Relation Review Disposition".
