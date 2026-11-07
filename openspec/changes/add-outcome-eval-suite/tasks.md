## 1. Suites

- [x] 1.1 Drive CL-Bench `database_exploration` through its Task API with arms N, F and E, scored by upstream code.
- [x] 1.2 Build the planted wrong-note suite: governed and plain vaults with identical page bodies, a read-only answerer and a blind judge.
- [x] 1.3 Build the Harvey LAB suite: task sampling, document conversion, arms S, W and WG, and a blind criteria judge.
- [ ] 1.4 Add the primary arms as shipped (CL-Bench P, LAB WP: plugin, skills, hooks and the balanced custom-instruction block) and keep the bare-MCP, hook-only and graph-off arms as labelled ablations.

## 2. One command and the gate

- [x] 2.1 Add `membench outcome fetch|run|report|panel` and pin the upstream commits in one lock file.
- [x] 2.2 Gate held-out runs with the family release gate and the receipt's frozen digests; label every report.
- [x] 2.3 Add the Codex judge backend and record the judge model id in every judgement file.
- [x] 2.4 Write the release panel to `docs/benchmarks/outcome/<version>.json` and `.md`, paired against the previous version.

## 3. Registration

- [x] 3.1 Register f35 to f37 in PREREGISTRATION §1 and §7 sequence 9, the registry and its mapping, with receipt `amendment-2026-11-outcome-evals.v1.json` freezing the held-out inputs.
- [x] 3.2 Extend the amendment-governance tests to the ninth link.

## 4. Proof and delivery

- [ ] 4.1 Test that an actor environment cannot reach live state or the user's MCP configuration, that a judge item carries no arm identity, and that CL-Bench scoring agrees with an oracle replay.
- [ ] 4.2 Run scoped tests, the full suite, strict OpenSpec validation and the privacy gate; deliver one PR with an independent review.
