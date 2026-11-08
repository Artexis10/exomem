## 1. Specification

- [x] 1.1 Write the proposal, the design and the two deltas. Validate strictly with the CI-pinned OpenSpec.

## 2. Product

- [x] 2.1 Add the learning instruction text once in the package, beside the prominence constants.
- [x] 2.2 Add the setup step for Claude Code and Codex: consent prompt, marked span, explicit non-interactive flag, quiet-prominence skip, re-run update and uninstall removal.
- [x] 2.3 Show the text in the assistant guide, the remote quickstart and the prominence guide, and add the doc-copy check.

## 3. Benchmark

- [x] 3.1 Make the CL-Bench P arm lay the packaged text in the workspace `CLAUDE.md` and record its digest and place in the manifest.

## 4. Proof

- [x] 4.1 Add tests for consent, marked-span update, scripted no-write, quiet-prominence skip and uninstall, each watched failing on the unfixed code.
- [ ] 4.2 Run a CL-Bench development round on the candidate: P against the stored PP, F and N rounds on the same questions. Report writes, recalls, refusals and the paired score differences.

## 5. Delivery

- [ ] 5.1 Review, merge and release.
