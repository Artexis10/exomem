## 1. Replies

- [x] 1.1 Project a minimal compact write reply and move operational fields under `diagnostics.operational` for `response_detail=full` (`mutation_terminal`, test: `tests/test_mutation_terminal.py` superset tests).
- [x] 1.2 Remove `due_state` and `vocabulary_delta` from write replies and `due_state` from search replies.
- [x] 1.3 Shape the default search reply as `user_summary`, `hits` and one conditional `notice`, naming each page once.
- [x] 1.4 Turn the capture-sweep rule into one `next` sentence and keep `unpaged_mentions` as data.
- [x] 1.5 Return MCP refusals as `{error: {code, message, retry?, candidates?}}` with the old envelope under `diagnostics`.

## 2. Teaching

- [x] 2.1 Rewrite the bootstrap post-write guidance, the due-state entry and the capture-sweep entry for the new reply.
- [x] 2.2 Update the scaffold references `engagement.md` and `mutation-results.md`, then regenerate the skill contract and the plugin copies.

## 3. Proof

- [x] 3.1 Record reply bytes and keys for search (warm and cold), a successful add, an unlinked-page refusal and a busy refusal on main and on the branch, compact and full.
- [x] 3.2 Update the tests that pinned operational keys or the old error envelope, and delete the two tests of removed carriers.
- [ ] 3.3 After release, replay a real-agent session on the branch and check that the agent reads `user_summary`, follows `next` and uses `retry` without extra turns.
