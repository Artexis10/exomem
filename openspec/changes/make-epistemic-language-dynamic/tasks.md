## 1. Specification

- [ ] 1.1 Write the proposal, the design and the deltas. Validate strictly with the CI-pinned OpenSpec.
- [x] 1.2 Record the owner's choice for degree of belief (levels plus sourced numbers, 2026-11-08). Then add the MODIFIED deltas for the bootstrap commitments and the verdict remediation text.
- [ ] 1.3 Agree the split with the neighbours: `relates_to` retyping, the single-store edge table, the sensed epistemic model and approved procedures.

## 2. Behaviours and dimensions

- [ ] 2.1 Declare the closed behaviour set in the registry schema, and the dimension registry with shipped defaults.
- [ ] 2.2 Give every shipped relation family its behaviours and dimension. Replace `_LIFT_RELATION_FAMILIES` and `MEMBERSHIP_FAMILY` with behaviour reads, and prove identical lift and membership output on a fixture vault.

## 3. Kinds and verdicts

- [ ] 3.1 Move built-in kinds and verdicts into the shipped pack with behaviours and dimensions. Make every code list read the registry.
- [ ] 3.2 Add the definition and procedure kinds, the `defines`, `is_a`, `precedes` and `follows` relations, and the `at` key on timeline events.

## 4. New unit attributes

- [ ] 4.1 Add `mood` and `holder`, with validation and retrieval that keep norms and views distinct.
- [ ] 4.2 Add degree of belief in the owner's chosen shape, with validation that names `holder` and `basis`.
- [ ] 4.3 Add quantities: `value`, `unit` and `uncertainty`.

## 5. Language and teaching

- [ ] 5.1 Add labels per language to every registry entry, and parse labels as aliases of their key.
- [ ] 5.2 Teach the language from the registry in bootstrap and the skill scaffold, keeping the scaffold generic.

## 6. Proof and delivery

- [ ] 6.1 Prove each slice on real models: agents choose the right word on realistic and hostile inputs.
- [ ] 6.2 Review, merge, release, and archive this change.
