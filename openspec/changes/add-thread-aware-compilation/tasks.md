# Tasks: add-thread-aware-compilation

No code lands until the orchestrator rules on the "Needs ruling" items in the PR. Every behaviour task is red-first: write the test, show it failing, then implement.

## 0. Design ruling

- [ ] 0.1 Orchestrator rules on the open decisions listed in the PR body:
  - `focus` as current-turn evidence;
  - assistant entries in `recent`;
  - the call-ledger hash;
  - the latency bound's scope;
  - one surface change or two;
  - the branch name.

  Amend these artifacts to match the ruling, then run `openspec validate --all --strict`.

## 1. S0: pre-registration and baseline (no product change)

- [ ] 1.1 Red: `tests/test_context_activation_conversation_fixtures.py`. It proves the group has the following, and that editing any gold list changes the digest:
  - twelve rich turns and twelve multi-turn conversations;
  - one twin per case;
  - at least three drowning and three topic-switch cases;
  - one withheld-versus-absent pair;
  - gold, poison, must-include, must-exclude, expected status and expected `carried_by`;
  - invented names only;
  - no fixture turn verbatim in any corpus page.
- [ ] 1.2 Author the fixtures:
  - `FixtureCase.conversation` (optional) and the group ids;
  - the corpus additions through supported writers: entity pages, hubs, one Records collection, and one governed page withheld from a restricted audience;
  - a new `FIXTURE_SET_ID` and digest, pinned in the test.

  Commit this before any scored run.
- [ ] 1.3 Scorer arms a to d in `benchmarks/membench/utility/context_activation.py`:
  - drowning is a case failure;
  - byte-identity scoring for the withheld pair;
  - the mechanism-removal verdict from arm (a).

  Add the new test module to `tests/harness_modules.txt` if it imports `benchmarks/`.
- [ ] 1.4 Run arm (a) on current `main`. Record the baseline manifest (with the fixture digest) and confirm that the incident classes fail. Arms b to d are expected to be refused as unknown arguments.

## 2. S1: argument, bounds, privacy, promotion and tie-break

- [ ] 2.1 Red: bounding tests covering:
  - oversized, malformed and empty input;
  - `generation.conversation` values;
  - byte-identity of a no-conversation packet against the pre-change packet, apart from the new field;
  - door parity across MCP, CLI and REST.
- [ ] 2.2 Red: privacy tests covering:
  - a sentinel phrase absent from every state file, and its sha256 absent from every file except the call ledger;
  - cache bypass;
  - no hot-profile event from refs;
  - a token minted without conversation-only anchors;
  - activation-log fields limited to presence, counts and the generation value.
- [ ] 2.3 Red: the withheld-ref twin is byte-identical to the absent one, `generation` included.
- [ ] 2.4 Red: the `focus` segment.
  - It carries worded kinds only, with no second recall or embedding.
  - Segments never pair.
  - A referential turn stays referential when a `focus` accompanies it.
- [ ] 2.5 Red: the `conversation` qualifier.
  - It covers promotion of a partial second domain and nothing resolving alone.
  - A name split across entries does not match.
  - Only the newest three user and two assistant entries are read.
  - `conversation` and `continuity` count once together.
- [ ] 2.6 Red: the tie-break. Exactly one competitor with `conversation` settles the turn; two keep the ambiguity.
- [ ] 2.7 Red: the drowning guard.
  - A long conversation about one subject does not resolve that subject for a turn about another.
  - Promoted material stays within a third of the budget and is served after the turn's own anchors.
- [ ] 2.8 Implement in `working_set.py`, `working_set_resolve.py` and `working_set_runtime.py`, plus the `commands.py` leaf and CLI flags. Register the timing span `working_set.conversation`, with the reserve-skip fallback.
- [ ] 2.9 Gates:
  - scoped pytest on the touched modules;
  - `uvx ruff check --select F src tests`;
  - the privacy gate;
  - `generate-capabilities.py --check`.

## 3. S2: anaphoric carry

- [ ] 3.1 Red: the anaphor set covers pronouns, possessives, demonstratives, the shipped follow-up markers, ordinals plus "one/option", and "former/latter".
  - A long anaphoric turn is carried from the newest resolving user entry.
  - The newest subject beats an older one.
  - Two anchors in that entry make the turn ambiguous.
  - Refs-only never carries.
  - A turn that names its own subject is not carried.
  - The carried anchor is `partial` and absent from the token.
  - Existing recency, continuity and follow-up carries take precedence.
- [ ] 3.2 Implement the carry walk. Rerun the S1 tests and the existing continuity, keyless and follow-up suites unchanged.

## 4. S3: hooks

- [ ] 4.1 Record transcript fixtures. Use Claude Code JSONL, invented content only, covering user and assistant text, a tool call and result, a thinking block, an injected Exomem block, and the current prompt already appended. Record a Codex rollout fixture once its `UserPromptSubmit` payload is verified to carry `transcript_path`, or record that it does not.
- [ ] 4.2 Red: hook tests covering:
  - which entries are extracted, what is excluded and the entry order;
  - refs taken from call arguments only;
  - no `focus`;
  - the 50 ms budget and fail-silent behaviour;
  - one retry without the field on an old service;
  - no conversation bytes in hook state;
  - an unchanged injection when no conversation is sent.
- [ ] 4.3 Implement in `src/exomem/_hooks/exomem_retrieve_nudge.py`, then regenerate the plugin mirror (`exomem package-skills --plugin-root plugins/claude-code`) and pass `tests/test_plugin_sync.py`.

## 5. S4: instructions and surface rollout

- [ ] 5.1 Red: the server instructions contain the pinned sentence, keep "verbatim", and stay within the existing length test. The tool description states the bounds. The scaffold line is updated, and `tests/test_scaffold_no_leak.py` passes.
- [ ] 5.2 Regenerate the derived surfaces:
  - `scripts/dump-tool-schemas.py`, which updates the schema fixture and the tool-surface contract;
  - the plugin and hosted trees (`scripts/hosted-plugin.py`);
  - the v5 candidate;
  - `docs/capabilities.md`;
  - the README tool table.
- [ ] 5.3 Set `pending_tool_surface_sha256` and `refresh_required` in `deploy/chatgpt/personal-plugin-contract.json` per `docs/remote-quickstart.md`. Hand the owner the refresh steps for the ChatGPT Personal Plugin and the claude.ai connector. Promote the digest only after the owner verifies it.

## 6. S5: acceptance and latency

- [ ] 6.1 Run the `conversation` group on arms a to d against the pinned digest. Publish per-case, per-anchor-kind metrics with their duals and no aggregate. Arm (a) must be red and arms b to d must meet the floors.
- [ ] 6.2 Pin `working_set.conversation` p95 ≤ 60 ms and warm activation p95 ≤ 1,000 ms (maximum-size conversation, synthetic reference corpus) in the CI latency gate, with the measured numbers recorded in the manifest.
- [ ] 6.3 Sync the deltas into the canonical specs and archive with `openspec archive` in the same delivery that completes 6.1 and 6.2. Run `openspec validate --all --strict` before and after.
