## 1. Assistant-Facing Docs

- [x] 1.1 Rewrite `docs/ai-assistant-guide.md` around simple agent actions, client setup, skip/search rules, and the required examples.
- [x] 1.2 Rewrite `docs/vs-built-in-memory.md` to state the native assistant memory vs. Exomem boundary and what belongs in neither.
- [x] 1.3 Add a non-CLI-comfortable first-run path to `QUICKSTART.md` with assistant handoff and verification steps.

## 2. Scaffold Guidance

- [x] 2.1 Update `src/exomem/_scaffold/_Schema/SKILL.md` so agents use a simple front door before internal categories.
- [x] 2.2 Update relevant scaffold references for operation, write-scope, and supersession wording where examples need to match the new boundary story.
- [x] 2.3 Mirror scaffold schema changes into `tests/fixtures/Knowledge Base/_Schema/`.

## 3. Verification

- [x] 3.1 Run focused tests for scaffold leaks, schema parsing, and bootstrap contract.
- [x] 3.2 Run additional setup/adoption/stale-review tests if touched docs or fixtures indicate risk.
- [x] 3.3 Mark tasks complete and record the implementation conclusion in Exomem.
