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
  - Interim, from the installed service's activation log on release 0.201.0 (2026-11-24 11:44 to 2026-11-25 11:05 UTC, served and abstained, boundary `activation-leaf-v1`): the skills and catalogue stages cost a median 15 ms and 32 ms of a 222 ms hook activation (n=351, a median 28% of the call), and 50 ms and 38 ms of a 7859 ms MCP activation (n=279, about 1%).
  - Still open: a paired before and after on the recall latency instrument. Whole-call percentiles from live logs do not answer this task, because the traffic mix between hook and MCP calls changes from day to day.

## 3. Deliver context on every turn

- [x] 3.1 From the 1.1 results, choose the start-of-turn delivery for the hook and the server instructions; add the `retrieve-inject-hook` delta before code.
- [ ] 3.2 Implement and prove it on follow-up turns with real models, including a provider outage and a Codex case.
  - Implementation landed: the native-mode hook fetches the changed session context with one empty-turn call, and the nudge and server instructions ask for recall per new subject. Hook tests cover its transport.
  - Follow-up turns, proved with a GPT actor on each arm's own server and real hook output, 3 repeats. Before scoping, the per-subject nudge made the agent recall on 7 of 7 general first turns. With the nudge scoped to the user's work or life, it recalled on 0 of 6. Personal follow-up turns recalled 6 of 6, and subject switches recalled on the switching turn.
  - Provider outage, proved with the same actor against a server whose every call fails. The agent made at most 3 tool calls a turn and did not loop. It said memory was unavailable and asked for the missing facts. It answered general turns with no memory call.
  - Still open: the Codex case, which needs a Codex client run.

- [x] 3.3 Evaluate pick-by-problem routing tables (a note whose rows map a problem to the method or source to use) as a method carrier beside skills; specify before code.
  - Evaluated and specified in `add-domain-profiles-and-curation`, design D8 (round R3): a routing table is an ordinary vault skill, and the catalogue lists it. No product code and no `route` items.

## 4. Verify and deliver

- [x] 4.1 Run real-model cases: a skill-shaped task in a client without native skills loads the vault skill before acting; an unrelated task loads none.
  - GPT actor, client without native skills. The skill-shaped task read the vault skill before drafting in 5 of 5 runs. The unrelated task and the near-miss task loaded no skill in 0 of 8 runs. On the three-page fixture, the arm before the skills block also found the skill, so these cases prove the behaviour but do not separate the arms.
- [x] 4.2 Run the full corpus, independent review, CI, merge and the ordinary release.
  - The catalogue shipped through an independent review, its PR checks and release 0.201.0. A Full-only budget failure after merge was fixed forward, and the Full run on the release-cut commit passed.
- [x] 4.3 Verify on the installed service: a new conversation receives the catalogue and a matching turn loads the skill.
  - Installed 0.201.0, 2026-11-24: a recall in a new conversation, on a turn that matches a vault skill, returned `skills` ready with that skill listed, its `advice`, and `next_action` `skill_check`; reading the listed ref returned the whole skill. The first recall after the install abstained after 302 s while the index warmed (readiness overran its 50 s budget); the retry was served.
- [ ] 4.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.

## 5. Method activation from ordinary turns

- [x] 5.1 Select skills and Records collections with the bounded method-fit judgment over their authored descriptions, served first and bounded by `method_fit_max_items`; verify selection, none, a failed call, an unsupplied label and no placement.
- [x] 5.2 Carry each selected skill's contract under `method_contract_max_chars` in the MCP packet and the start-of-turn hook; verify whole, cut and over-allowance cases.
- [x] 5.3 Report method use evidence in the owner served-context readout from the served rows and the local read and write logs; verify followed, not observed and disabled-log cases.
- [x] 5.4 Add the no-magic-word fixtures (technology discussed, experiment approved, outcome reported, Exomem failure, each with a negative twin) as session-close scenarios with scorer-only keys; record the packet-level run against base and candidate.
- [x] 5.5 Run the fixtures through the session-close bench on real models; record whether the agent followed the contract and wrote to the keyed destination.
- [x] 5.6 Compare method selection with no judgment, a local placement and an API placement on the same cases; record per-case exact match with intervals, and recommend a placement.
- [x] 5.7 Verify on the installed service: an ordinary turn serves the matching vault skill with its contract, and the owner readout shows its use evidence.
  - Installed 0.201.0, 2026-11-24: with the selector off, the turn served the skill in the catalogue and its whole contract on read. The owner `served_context` readout then showed that skill served 1 and opened 1 (observed), with followed unknown because the skill names no write destination.

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

## 7. Task leads rank like search

- [x] 7.1 Order task leads by the reciprocal-rank fusion of the discovery lanes, at search's fusion constant and lane depth; verify that a page both readings rank leads when more keyword matches than leads crowd it.
- [x] 7.2 Replay the 2026-11-24 broad-question miss and the development cases on a vault copy, main against the fix; record where each needed page lands and the task-discovery latency.
  - Model-free replay on a production-sized vault copy: release 0.202.0 against the same release with this change, every lane warm, main run again last to show drift.
  - The 2026-11-24 question moved from lead 7 to lead 3.
  - Development cases (24): the needed page reached the first five leads in 4 on main and 5 with the change, +0.04 [−0.08, +0.17]. One case gained a page from no lead to lead 1. One case lost a page that only the keyword lane ranked, from lead 5 to below the first five.
  - Frozen held-out cases (10): 4 against 5, +0.10 [0, 0.30], one gained and none lost.
  - Task-discovery median on the development cases: 886 ms against 860 ms, −5.5 ms [−87, +54]. Main's own rerun drifted +145 ms.
  - On the held-out cases: +97 ms [+6, +301], with no drift rerun, so it does not separate the change from drift. Ordering itself costs under 1 ms. A paired timing of the keyword lane alone at both depths, on synthetic 4,000- and 16,000-page vaults, showed no difference.
  - Run records stay outside the repository, with the private cases.
- [ ] 7.3 Verify on the installed service that the broad question from the 2026-11-24 miss serves its compiled note among the rendered leads.
