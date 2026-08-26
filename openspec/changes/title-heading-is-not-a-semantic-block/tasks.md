# Tasks: title-heading-is-not-a-semantic-block

## 1. Reproduce

- [x] 1.1 Test that a page titled exactly with a block-type name — `Source`, `Decision`, `Open Question` — yields its real `##` blocks rather than one level-1 block covering the file.
- [x] 1.2 Test the control: a page whose title is ordinary prose, and one whose title carries a colon and a filename, parse exactly as they do today.

## 2. Scope blocks to level 2 and deeper

- [x] 2.1 Stop typing level-1 headings as semantic blocks in `semantic_blocks.py`.
- [x] 2.2 Test that deeper nesting is unchanged: a `### Decision` inside a `## Claim` stays in the claim's body.
- [x] 2.3 Test that a level-1 heading still closes an open block, so a `## Claim` before a later `# Title` does not run past it.

## 3. Verification

- [x] 3.1 Run the semantic block, unit, index, graph and census suites.
- [x] 3.2 Run `openspec validate title-heading-is-not-a-semantic-block --strict` and `--specs --strict`.

## 4. Closure

- [ ] 4.1 Once merged, sync the delta into `openspec/specs/` and archive with `openspec archive`, re-running `openspec validate --all --strict` before and after.
