# Tasks

## 1. Measure the baseline

- [ ] 1.1 Replay real operator turns against the installed build. Record, per case, whether the needed page reached the response, in which block and rank, and the latency. Keep cases and keys outside the repository.
- [ ] 1.2 Freeze a held-out third of the cases before any iteration and record its digest.

## 2. Recognize and catalogue skills

- [x] 2.1 Add the `skill` flag and frontmatter description to the context catalogue; verify recognition, the malformed case, the Source exclusion and a catalogue rebuild from an older version.
- [x] 2.2 Serve the `skills` block in maps responses with its own bound, continuity generation and `unchanged` state; verify first delivery, the unchanged turn, an edited skill and the off switch.
- [x] 2.3 Accept `native_skills` and omit matching names from the catalogue only; verify a projected method still appears for an omitted skill.
- [x] 2.4 Project admitted skill pages as `kind: "skill"` method items with the full description; verify against a real turn that names no skill.
- [x] 2.5 Keep a skill's other files one `read_page` away by the paths its body names, with no directory listing: a listing that is both bounded and blind to hidden pages needs an indexed folder read the catalogue does not have.
- [x] 2.6 Render the catalogue and skill methods in the start-of-turn hook and pass the client's native skill names; verify the hook output for a Claude Code and a Codex home.
- [ ] 2.7 Prove recall latency before and after on the recall latency instrument.

## 3. Deliver context on every turn

- [ ] 3.1 From the 1.1 results, choose the start-of-turn delivery for the hook and the server instructions; add the `retrieve-inject-hook` delta before code.
- [ ] 3.2 Implement and prove it on follow-up turns with real models, including a provider outage and a Codex case.

- [ ] 3.3 Evaluate pick-by-problem routing tables (a note whose rows map a problem to the method or source to use) as a method carrier beside skills; specify before code.

## 4. Verify and deliver

- [ ] 4.1 Run real-model cases: a skill-shaped task in a client without native skills loads the vault skill before acting; an unrelated task loads none.
- [ ] 4.2 Run the full corpus, independent review, CI, merge and the ordinary release.
- [ ] 4.3 Verify on the installed service: a new conversation receives the catalogue and a matching turn loads the skill.
- [ ] 4.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.
