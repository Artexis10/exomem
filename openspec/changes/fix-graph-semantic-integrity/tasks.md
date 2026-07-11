## 1. Canonical Relation Contracts

- [x] 1.1 Add regression coverage proving canonical `## Relations` edges participate in contract inference, validation, and diff while generic links and unknown labels do not.
- [x] 1.2 Update memory-contract relation collection to include resolved `markdown_relation` edges without broadening to generic wikilinks or frontmatter edges.

## 2. Semantically Neutral Suggestions

- [x] 2.1 Add regression coverage for neutral shared-source and embedding-proximity candidates, preserved evidence/methods, explicit-source semantics, and non-mutation.
- [x] 2.2 Change similarity-only candidate types from `refines` to the governed symmetric `relates_to` relation without changing candidate ordering or response shape.

## 3. Verification

- [x] 3.1 Run focused schema, graph, registry, and relation-parser tests with embeddings disabled.
- [x] 3.2 Run lint, OpenSpec validation, and the broad lean test suite available in the worktree environment.
