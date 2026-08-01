## 1. Scope Declaration

- [x] 1.1 Add failing coverage: a scope document carrying the declaration compiles, and one omitting it is unchanged.
- [x] 1.2 Add failing coverage: a malformed declaration value is an error finding, not a silent default.
- [x] 1.3 Add the field to `Scope`, the compiler's recognised scope fields, and validation.

## 2. Evaluator Default

- [x] 2.1 Add failing coverage: an audience no standing rule names resolves to no disclosure for a declared scope, and to full release for an undeclared one.
- [x] 2.2 Add failing coverage: a rotated credential minting an unnamed audience id is denied identically.
- [x] 2.3 Add failing coverage: an authored rule still governs the audience it names, and the declaration does not lower it.
- [x] 2.4 Add failing coverage: a grant still raises above the default; an org cap still lowers; a declared purpose still only narrows.
- [x] 2.5 Add failing coverage: the owner reads a declared scope at full release.
- [x] 2.6 Add failing coverage: an item in both a declared and an undeclared scope is denied.
- [x] 2.7 Change the standing default in `_decide_at` to depend on the declaration and the audience, applying it only when the standing set is empty.

## 3. Surfaces And Indistinguishability

- [x] 3.1 Add failing coverage: an item denied by default is byte-identical to a missing one across `get`, `fetch`, recall, graph and media — reusing the same-input/varied-condition shape, not two different paths.
- [x] 3.2 Add failing coverage: `explain` names the declaring scope and does not attribute the outcome to a nonexistent rule.
- [x] 3.3 Wire the explanation through `inspection`.

## 4. Verification

- [x] 4.1 Run the governance, decision, membership and postfilter suites with embeddings disabled.
- [x] 4.2 Run the governance overhead and latency gates; confirm the empty-policy budget is unchanged.
- [x] 4.3 Run `ruff check`, `git diff --check`, and the scaffold leak gate.
- [x] 4.4 Run `openspec validate add-default-deny-scope-cap --strict`.
- [x] 4.5 Run the lean suite, then the full suite.
- [ ] 4.6 Independent adversarial review of the exact diff, with the reviewer rechecking its own findings after fixes.
