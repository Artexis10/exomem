Implementation merged in [PR #1616](https://github.com/Artexis10/exomem/pull/1616) as `b7188ca60f2eff15db512691235c0ad431bd3d24`.
Independent review approved head `6c9ddcfc4564ea5081ec8862323bdb4773d2f085` after the correction rounds.
[Full CI run 37689546133](https://github.com/Artexis10/exomem/actions/runs/37689546133) passed on that head, including installed-product and native Windows checks.
Production rollout remains unverified and belongs to the ordinary release owner.

## 1. Capture contract

- [x] 1.1 Correct kind refusals and count failures; verify no effects and explicit unknown counts.
- [x] 1.2 Bind the terminal UI exception separately; verify library callers still require a kind.
- [x] 1.3 Deprecate `other`, add the `unclassified` built-in, and refuse both as a reclassification target
- [x] 1.4 Restrict the bounded classification-debt advisory to the verified owner; verify exempt hosted callers receive none.
- [x] 1.5 Name the alternative in the URL refusal
- [x] 1.6 Carry corrective guidance and honest counts across hosted routes; verify an old-style request can recover.

## 2. Agent-facing text

- [x] 2.1 Version the bootstrap migration; verify every profile teaches the shared kind rule and historical descriptors remain unchanged.
- [x] 2.2 Remove `Other` as a destination from the scaffold references, the capture and ingest skills, `source-taxonomy.yaml`, the knowledge packs and the docs
- [x] 2.3 Regenerate current artifacts through their source generators; verify frozen hosted descriptors remain unchanged.

## 3. Proof

- [x] 3.1 Run capture, advisory, count-failure and historical recovery proofs; verify all four independent review findings close.
- [x] 3.2 Update tests that encoded the fallback contract
- [x] 3.3 Full pytest corpus green in pull-request CI

## 4. Delivery

- [x] 4.1 Independent review, merge, then sync this delta and run `openspec archive`
