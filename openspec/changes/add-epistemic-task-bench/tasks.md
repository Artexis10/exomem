## 1. Specification

- [ ] 1.1 Write the proposal, the design and the delta. Validate strictly with the CI-pinned OpenSpec.
- [ ] 1.2 Agree the design with the owner, including the arms, the dimensions, the decision rules and the cost.

## 2. Cases

- [ ] 2.1 Brief an independent authoring lane. It writes three projects of 12 sessions, with 2 probes per dimension per project and their keys, into evaluator custody.
- [ ] 2.2 Fix the split (one dev project, two held out), and record the case digests.

## 3. Harness

- [ ] 3.1 Add the `epistemic` suite to `membench outcome`. It reuses the CL-Bench session runner with lockstep scheduling across arms, the f37 judge with the behaviour schema, and the gate.
- [ ] 3.2 Add the report: paired probe correctness clustered by project, per-dimension rows, per-window stability, key and judge disagreements, and turns, tokens and cost.

## 4. Dev rounds

- [ ] 4.1 Run the dev project with all four arms in one window, and repair keys and probes that the disagreements expose.
- [ ] 4.2 Run arm N with the full transcript on the dev project, to check that every probe is solvable without memory.

## 5. Registration and decision

- [ ] 5.1 File the preregistration amendment that adds f38, with its held-out digests and decision rules, and record the owner's acknowledgment.
- [ ] 5.2 State the cost, then run the two-window held-out decision run.
