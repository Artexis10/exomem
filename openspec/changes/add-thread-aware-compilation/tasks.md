# Tasks: add-thread-aware-compilation

The orchestrator ruled on the design on 2026-09-29; the rulings are recorded in `design.md`. Every behaviour task is red-first: write the test, show it failing, then implement. Run tests scoped to the touched modules with `CUDA_VISIBLE_DEVICES= XDG_STATE_HOME=$(mktemp -d) uv run pytest -q -p no:cacheprovider <files>`.

## 0. Design ruling

- [x] 0.1 The orchestrator ruled on `focus` as current-turn evidence with origin labels, assistant entries in `recent`, the call-ledger hash, the latency scope, S1+S4 as one surface change, the branch name, and the attachment cues. The artifacts are amended to match, and `openspec validate --all --strict` passes.
- [ ] 0.2 The orchestrator rules on retiring `claude/keyless-thread-continuity` unmerged (design D9). Until then, task 3.0 stands.

## 1. S0: pre-registration and baseline (no product change)

- [x] 1.1 Red: `tests/test_context_activation_conversation_fixtures.py`. It proves the group holds everything below, and that editing any gold list changes the digest:
  - twelve rich turns and twelve multi-turn conversations;
  - one twin per case;
  - at least three drowning cases and three topic-switch cases;
  - one withheld-versus-absent pair;
  - two attachment cases with cue-only `focus`, plus a twin;
  - gold, poison, must-include, must-exclude, expected status, expected `carried_by` and expected `origin`;
  - invented names only;
  - no fixture turn verbatim in any corpus page.
  - Evidence: `tests/test_context_activation_conversation_fixtures.py` was written first and failed at collection (`ModuleNotFoundError: epistemic.corpora.context_activation_conversation`), then passed with 31 tests once the fixtures existed (commit d30a97e6).

- [x] 1.2 Author the fixtures and pin their digest:
  - add `FixtureCase.conversation` (optional) and the group ids;
  - make the corpus additions through supported writers: entity pages, hubs, one Records collection, and one governed page withheld from a restricted audience;
  - add a new `FIXTURE_SET_ID` and digest, pinned in the test.

  Commit this before any scored run.
  - Evidence: `benchmarks/epistemic/corpora/context_activation_conversation.py`, `FIXTURE_SET_ID = context-activation-conversation-v1`, digest `374056f5fc22d9a75249b83162d141a5b293b2fc5fc68071ebcad88a2fd5170f` pinned in the test; 55 cases (12 rich turns, 13 conversations, 2 attachments, the withheld pair, 26 twins). The English digest `a49d85f4…` is unchanged and asserted. The sibling `ConversationCase` carries `conversation`, so the digest-pinned English `FixtureCase` is untouched. Entity, hub, resource and Records pages use the English module's writers; the withholding scope and ceiling-0 rule are written as the governance tests write them, because `govern_memory` commit refuses on a cold fixture vault (`prepared composite does not match`). Committed before any scored run (d30a97e6).

- [x] 1.3 Add scorer arms a to d in `benchmarks/membench/utility/context_activation.py`:
  - drowning counts as a case failure;
  - the withheld pair is scored for byte identity;
  - arm (a) gives the mechanism-removal verdict;
  - `origin` is checked against the expectation.

  Add the new test module to `tests/harness_modules.txt` if it imports `benchmarks/`.
  - Evidence: `benchmarks/membench/utility/context_activation_conversation.py` scores arms a to d per case and per anchor kind with duals and no aggregate; drowning fails a case outright; the withheld pair is scored for byte identity; the arm (a) mechanism-removal verdict and the `origin` check are in; the new test modules are in `tests/harness_modules.txt`. Sibling module, not a change to `context_activation.py`, mirroring the multilingual scorer.

- [x] 1.4 Run arm (a) on current `main` and record the baseline manifest with the fixture digest. Confirm that the incident classes fail and that the attachment cases abstain. Arms b to d are expected to be refused as unknown arguments.
  - Evidence: Arm (a) on `main` fails 9 of 13 multi-turn cases (mechanism-removal arm red); promotion V13-V14, tie-break V15-V16, anaphoric carry V17-V18 and topic switch V19-V21 all fail; both attachment cases abstain; all 26 twins, the three drowning cases and the refs-only case pass; 82 conversation-bearing arm b to d requests are recorded `refused: unknown argument`. Manifest: `baseline-arm-a.json`.

## 2. S1+S4: argument, evidence and surface, shipped as one surface change

- [ ] 2.1 Red: bounding tests covering:
  - oversized, malformed and empty input;
  - the `generation.conversation` values;
  - a packet without `conversation`, byte-identical to the pre-change packet apart from the new field, with every anchor labelled `origin = "turn"`;
  - no `working_set.conversation` span on a request without `conversation`;
  - door parity across MCP, CLI and REST.
- [ ] 2.2 Red: privacy tests covering:
  - a sentinel phrase absent from every state file, and its sha256 absent from every file except the call ledger;
  - the cache bypass;
  - no hot-profile event from refs;
  - the token never encoding conversation-only anchors;
  - activation-log fields limited to presence, counts and the generation value.
- [ ] 2.3 Red: a withheld-ref twin is byte-identical to the absent one, `generation` included.
- [ ] 2.4 Red: the `focus` segment.
  - It uses worded kinds only, with no second recall or embedding.
  - Segments never pair.
  - A referential turn stays referential when `focus` is present.
  - Anchors carry `origin` values `turn`, `focus` and `turn_and_focus`.
  - A `focus` origin is never `agent_choice`.
  - `focus` does not settle an ambiguity the turn segment resolved.
- [ ] 2.5 Red: attachment cues.
  - A content-free turn plus cue-only `focus` resolves with `origin = "focus"`.
  - The same turn without `focus` abstains.
  - A cue that names nothing leaves the packet unchanged.
  - Activation opens no media module, asserted by a guard on the media and vision imports during the call.
- [ ] 2.6 Red: the `conversation` qualifier.
  - It promotes a partial second domain.
  - It resolves nothing alone.
  - It never matches a name split across entries.
  - Only the newest three user and two assistant entries are read.
  - It counts once together with `continuity`.
- [ ] 2.7 Red: the tie-break. Exactly one competitor carrying `conversation` settles the turn; two competitors keep the ambiguity.
- [ ] 2.8 Red: the drowning guard.
  - A long conversation about one subject does not resolve that subject for a turn about another.
  - Promoted material stays within a third of the budget and is served after the turn's own anchors.
- [ ] 2.9 Red: the surface.
  - The server instructions contain the pinned sentence, including the attachment clause, keep "verbatim", and pass the existing length test. If the length test fails, apply design D5's fallback and update the spec in the same PR.
  - The tool description states the bounds, the attachment cues, the origin meaning, and that activation reads no attachment.
  - The scaffold line is updated, and `tests/test_scaffold_no_leak.py` passes.
- [ ] 2.10 Implement:
  - in `working_set.py`, `working_set_resolve.py` and `working_set_runtime.py`;
  - the `commands.py` leaf and CLI flags;
  - `server.py` (`SERVER_INSTRUCTIONS`);
  - the scaffold `SKILL.md`;
  - the timing span `working_set.conversation`, with the reserve-skip fallback.
- [ ] 2.11 Regenerate the derived surfaces:
  - `scripts/dump-tool-schemas.py`, which writes the schema fixture and the tool-surface contract;
  - the plugin trees and the hosted render (`scripts/hosted-plugin.py`);
  - the v5 candidate;
  - `docs/capabilities.md`;
  - the README tool table.

  Set `pending_tool_surface_sha256` and `refresh_required` in `deploy/chatgpt/personal-plugin-contract.json` per `docs/remote-quickstart.md`.
- [ ] 2.12 Run the gates:
  - scoped pytest;
  - `uvx ruff check --select F src tests`;
  - the privacy gate;
  - `generate-capabilities.py --check`.

  Then hand the owner the one refresh of the ChatGPT Personal Plugin and the claude.ai connector. Promote the digest only after the owner verifies it.

## 3. S2: the conversation carry and the precedence ladder

- [ ] 3.0 Re-run the D9 audit of `claude/keyless-thread-continuity` against current `main`:
  - compare the branch's test functions with main's;
  - diff `working_set_resolve.py`;
  - find the `:stranger` salt;
  - trial-merge in a scratch worktree.

  If a behaviour or test exists only on the branch, merge the branch into the build branch first, resolving every conflict toward `main`'s later contract (authenticated threads, the `withheld` exception). Include that merge in this PR's review. If nothing is residual and the orchestrator has ruled on 0.2, follow that ruling.
- [ ] 3.1 Red: anaphoric turns and the carry.
  - The anaphor set covers pronouns, possessives, demonstratives, the shipped follow-up markers, an ordinal plus "one" or "option", and "former" or "latter".
  - A long anaphoric turn is carried from the newest user entry that resolves an anchor, with `origin = "conversation"`.
  - The newest subject beats an older one.
  - Two anchors in that entry make the turn `ambiguous`.
  - A refs-only conversation never carries.
  - A turn that names its own subject is not carried.
  - The carried anchor is `partial` and absent from the token.
- [ ] 3.2 Red: the precedence ladder.
  - Referential recency, the continuity-thread resume and the shipped follow-up carry each still win, with `carried_by` unchanged.
  - The conversation carry wins over the retrieval carry.
  - A conversation carry that abstains `ambiguous` stops the retrieval carry.
  - Without `conversation`, every existing carry behaves byte-identically.
- [ ] 3.3 Implement the carry walk and the ladder position. Rerun, unchanged, the S1 tests and the existing continuity, keyless, follow-up, carry and hot-projection suites:
  - `test_working_set_continuity.py`
  - `test_working_set_keyless_continuity.py`
  - `test_working_set_carry.py`
  - `test_working_set_hot_projection.py`

## 4. S3: hooks

- [ ] 4.1 Record transcript fixtures with invented content only. For Claude Code JSONL, cover:
  - user and assistant text;
  - a tool call and its result;
  - a thinking block;
  - an injected Exomem block;
  - the current prompt already appended.

  Record a Codex rollout fixture once its `UserPromptSubmit` payload is verified to carry `transcript_path`, or record that it does not.
- [ ] 4.2 Red: hook tests covering:
  - which entries are extracted, what is excluded, and the order;
  - refs taken from call arguments only;
  - no `focus` sent;
  - the 50 ms budget, failing silent;
  - one retry without the field on an older service;
  - no conversation bytes in hook state;
  - origin labels rendered for `conversation`;
  - an all-`turn` packet rendered byte-identically to today.
- [ ] 4.3 Implement in `src/exomem/_hooks/exomem_retrieve_nudge.py`. Regenerate the plugin mirror with `exomem package-skills --plugin-root plugins/claude-code` and pass `tests/test_plugin_sync.py`.

## 5. S5: acceptance and latency

- [ ] 5.1 Run the `conversation` group on arms a to d against the pinned digest.
  - Publish per-case, per-anchor-kind metrics with their duals, and no aggregate.
  - Arm (a) must come out red, and arms b to d must meet the floors.
- [ ] 5.2 Pin these in the CI latency gate, with the measured numbers recorded in the manifest (maximum-size conversation, synthetic reference corpus):
  - `working_set.conversation` p95 ≤ 60 ms;
  - warm activation p95 ≤ 1,000 ms.

  Record separately that live-cell latency is owned by its own lane and was not regressed: a request without `conversation` records no conversation stage.
- [ ] 5.3 In the delivery that completes 5.1 and 5.2, sync the deltas into the canonical specs and archive with `openspec archive`. Run `openspec validate --all --strict` before and after.
