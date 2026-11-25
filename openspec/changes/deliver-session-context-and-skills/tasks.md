# Tasks

## 1. Measure the baseline

- [x] 1.1 Replay real operator turns against the installed build. Record, per case, whether the needed page reached the response, in which block and rank, and the latency. Keep cases and keys outside the repository.
- [x] 1.2 Freeze a held-out third of the cases before any iteration and record its digest.

## 2. Recognize and catalogue skills

- [x] 2.1 Add the `skill` flag and frontmatter description to the context catalogue; verify recognition, the malformed case, the Source exclusion and a catalogue rebuild from an older version.
- [x] 2.2 Serve the `skills` block in maps responses with its own bound, continuity generation and `unchanged` state; verify first delivery, the unchanged turn, an edited skill and the off switch.
- [x] 2.3 Accept `native_skills` and omit matching names from the catalogue only; verify a projected method still appears for an omitted skill.
- [x] 2.4 Project admitted skill pages as `kind: "skill"` method items with the full description; verify against a real turn that names no skill.
- [x] 2.5 Keep a skill's other files one `read_page` away by the paths its body names, with no directory listing: a listing that is both bounded and blind to hidden pages needs an indexed folder read the catalogue does not have.
- [x] 2.6 Render the catalogue and skill methods in the start-of-turn hook and pass the client's native skill names; verify the hook output for a Claude Code and a Codex home.
- [ ] 2.7 Prove recall latency before and after on the recall latency instrument.

## 3. Deliver context on every turn

- [x] 3.1 From the 1.1 results, choose the start-of-turn delivery for the hook and the server instructions; add the `retrieve-inject-hook` delta before code.
- [ ] 3.2 Implement and prove it on follow-up turns with real models, including a provider outage and a Codex case.
  - Implementation landed: the native-mode hook fetches the changed session context with one empty-turn call, and the nudge and server instructions ask for recall per new subject. Hook tests cover its transport.
  - Follow-up turns, proved with a GPT actor on each arm's own server and real hook output, 3 repeats. Before scoping, the per-subject nudge made the agent recall on 7 of 7 general first turns. With the nudge scoped to the user's work or life, it recalled on 0 of 6. Personal follow-up turns recalled 6 of 6, and subject switches recalled on the switching turn.
  - Provider outage, proved with the same actor against a server whose every call fails. The agent made at most 3 tool calls a turn and did not loop. It said memory was unavailable and asked for the missing facts. It answered general turns with no memory call.
  - Still open: the Codex case, which needs a Codex client run.

- [ ] 3.3 Evaluate pick-by-problem routing tables (a note whose rows map a problem to the method or source to use) as a method carrier beside skills; specify before code.

## 4. Verify and deliver

- [x] 4.1 Run real-model cases: a skill-shaped task in a client without native skills loads the vault skill before acting; an unrelated task loads none.
  - GPT actor, client without native skills. The skill-shaped task read the vault skill before drafting in 5 of 5 runs. The unrelated task and the near-miss task loaded no skill in 0 of 8 runs. On the three-page fixture, the arm before the skills block also found the skill, so these cases prove the behaviour but do not separate the arms.
- [ ] 4.2 Run the full corpus, independent review, CI, merge and the ordinary release.
- [ ] 4.3 Verify on the installed service: a new conversation receives the catalogue and a matching turn loads the skill.
- [ ] 4.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.

## 5. Method activation from ordinary turns

- [x] 5.1 Select skills and Records collections with the bounded method-fit judgment over their authored descriptions, served first and bounded by `method_fit_max_items`; verify selection, none, a failed call, an unsupplied label and no placement.
- [x] 5.2 Carry each selected skill's contract under `method_contract_max_chars` in the MCP packet and the start-of-turn hook; verify whole, cut and over-allowance cases.
- [x] 5.3 Report method use evidence in the owner served-context readout from the served rows and the local read and write logs; verify followed, not observed and disabled-log cases.
- [x] 5.4 Add the no-magic-word fixtures (technology discussed, experiment approved, outcome reported, Exomem failure, each with a negative twin) as session-close scenarios with scorer-only keys; record the packet-level run against base and candidate.
- [x] 5.5 Run the fixtures through the session-close bench on real models; record whether the agent followed the contract and wrote to the keyed destination.
- [x] 5.6 Compare method selection with no judgment, a local placement and an API placement on the same cases; record per-case exact match with intervals, and recommend a placement.
- [ ] 5.7 Verify on the installed service: an ordinary turn serves the matching vault skill with its contract, and the owner readout shows its use evidence.

## 6. Catalogue-first activation

- [x] 6.1 List visible Records collections in the catalogue beside skills, with `kind`, and prove that reading an item's ref returns the whole page.
- [x] 6.2 Keep the listing turn-independent and name an overflowing catalogue's unlisted items of the turn's touched domains, then those the task context reached, in `turn_items`; verify fit, overflow, no-domain and held-across-turns cases.
- [x] 6.3 Serve `advice` in the catalogue and apply the no-hook lever the tool-only dev run supports (next action, task projection or server instructions); name skills and collections in agent-facing text.
- [x] 6.4 Seed the session-close bench warm (catalogue built before the actor's first recall), count warming abstentions, and let an arm carry a selector placement.
- [x] 6.5 Decide the catalogue shape on a fresh held-out set with a tool-only real client: catalogue alone against catalogue plus the selector, per-case exact with intervals; keep the selector off by default.
- [x] 6.6 Ask for recall on a later turn that opens a subject from the user's life or work, in clients without hooks, through the server instructions within their 900-character bound; prove it with a tool-only real client on the cases that exposed the miss and on fresh cases, with general-only negatives.
  - Server instructions only; proved on fresh cases and the scale set (design, "A later subject in a client without hooks"; run record `subject-switch-s1`).
- [x] 6.7 Make the shipped skill's description ask for recall on subjects of the user's own life or work, for clients that defer tools and hide server instructions; prove it on the incident regression cases and on fresh cases with a client that behaves that way.
  - Skill description only; proved in the bench's `deferred-skill` client mode on the incident regression cases and on fresh cases with the off level unchanged (run records `deferred-skill-description-s1` and `-s2`; design, "A client that defers tools and hides server instructions"). The live claude.ai check follows the release.
- [ ] 6.8 Name every item an overflowing catalogue could not list, within `skills_names_max_chars`, so a collection that declares no topic and shares no word with the turn still reaches the agent; prove it on the installed service with the turn that exposed the miss.
