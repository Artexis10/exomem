# Tasks

## 1. Provenance on compact observations

- [x] 1.1 Red test: a compact `add_observation` with `links: [{relation: derived_from, page: <Source>}]` commits, the page `sources` gains the Source once, and the outcome names it.
- [x] 1.2 Lift relations whose registry entry declares a forward `frontmatter_fields` mapping; keep the refusal for any other relation, and for a mix.
- [x] 1.3 Update the bootstrap and scaffold text that says compact observations cannot carry typed relations.

## 2. Applying a saved Source

- [x] 2.1 Red test, then add the conditional `next_action` to a committed `save_source` reply.

## 3. Semantic routing (measured, dropped)

- [x] 3.1 Calibrate on the maintainer's vault, read-only, and decide: dropped (design, "No semantic write-time routing").

## 4. Delivery

- [ ] 4.1 Scoped tests, the public-artifact privacy gate and the strict OpenSpec gate; one independent review; merge.
- [ ] 4.2 After release and install, run the 2026-11-23 real-model replay (operator approval for spend) and update its row through governed tools.
