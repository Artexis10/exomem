## 1. Clause

- [ ] 1.1 Write the candidate `SERVER_INSTRUCTIONS` text of at most 900 characters: the routing clause joined to the recall-loop sentence, with other wording shortened (design D1, D2). List each instruction it shortens, and record the text's SHA-256 in the evaluation protocol.

## 2. Evaluation

- [ ] 2.1 Settle how arm B's client receives the candidate in place of today's text, and check each arm's recorded system prompt (design D3).
- [ ] 2.2 Development round against a serving brief, with one case per shortened instruction (design D3), recorded in the personal eval-run collection.
- [ ] 2.3 Decision run on held-out subjects under an operator-agreed budget and L, recorded the same way.

## 3. Ship (only if 2.3 meets the decision rules in design D3)

- [ ] 3.1 Replace `SERVER_INSTRUCTIONS` with the candidate. Keep the tests that pin its delivery to every client surface and its 900-character bound passing.
- [ ] 3.2 Release, and prove a return-to-subject request from a real client reads the brief first.
