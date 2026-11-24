# Tasks

Start after `capture-structured-work-automatically` merges. This change builds on its automatic create, `new_fields` and incident-suite harness.

## 1. The restructure leaf

- [x] 1.1 Add `restructure` to `change_records` and `change_plans` with the closed step set, `preview`, `undo` and an `origin` that may be `declined`. Implement it once in the collection substrate, on the store (`collection_store/writer.py`) and on file-only collections (`records.py`). File-only identity moves are 1.7, and a split on an enrolled store is 1.8.
- [x] 1.2 Before commit, apply the steps, apply their inverse, and compare with the prior manifest and rows. Refuse naming the step and rows on any difference. Validate every dependent and refuse naming it.
- [x] 1.3 Record the inverse steps, moved-row origins and folded-row prior versions. Make `undo` run the same check and write its own history entry.
- [x] 1.4 Refuse a restructure or an undo that writes and retracts more than 256 rows together, a moved row counting twice, or that reaches a collection of more than 4,000 current rows, with `RESTRUCTURE_TOO_MANY_ROWS`, naming the count and a way forward, and changing nothing. A store collection counts its rows in SQL before it loads one. A resumable job for larger restructures is a follow-up change (design decision 8).
- [x] 1.5 Decide whether the restructure's history event needs a new reader floor. Decided: store schema 21 adds the shape-history table (schema 20 is the store derivation queue), and an older release refuses a schema-21 store as newer than it supports, as for every earlier store schema. No upgrade-barrier entry is added; no store schema bump has one. The CI pin of the release one schema back names the newest published release; move it to the first release with schema 20 once one ships.
- [x] 1.6 Test each step and its undo on a store collection, and each step but the identity moves of 1.7 on a file-only collection. Add one test each for a lossy retype, a conflicting merge, an undo that later rows make lossy, and a broken saved view.
- [x] 1.7 File-only identity moves are not built; they refuse `RESTRUCTURE_MOVE_UNAVAILABLE` in one place. Markdown-only collections are retired by a follow-up change that enrols new vaults in the store and migrates existing collections.
- [x] 1.8 Admit a split's new collection on a store that an ownership marker enrols, so `split_out` stops refusing `RESTRUCTURE_MOVE_UNAVAILABLE` there. The admitted create publishes the marker after its commit, so the split's moves and that admission need one recoverable order.

## 2. Retirement

- [x] 2.1 Implement `retire`, `restore`, `retire_field` and `restore_field`. Keep every row, value and history entry.
- [x] 2.2 Remove retired collections from `search_memory`, `recall_context`, the Records routing advisory, `collection_candidate` and the default inventory through one flag, the manifest's `lifecycle`, which each surface reads once per manifest. Report the retired count; list them on request with `include_retired`.
- [x] 2.3 Answer a read by identity, marked retired, with the successor. Refuse a new row with a pointer to the successor or `restore`. Refuse `add_record` `new_collection` on a retired title.
- [x] 2.4 Test that a retired collection leaves search and stays readable, that restore returns it, and that a retired field keeps its values.

## 3. Automatic bounds and shape history

- [x] 3.1 Add `automatic: true` to `restructure`. Check the bounds in `automatic_collection.py`, beside the creation bounds, reusing `MAX_FIELDS`. Refuse `AUTOMATIC_RESTRUCTURE_REFUSED` naming the bound and the plain-words way forward.
- [x] 3.2 Write one shape-history entry per create, restructure, undo and `new_fields` addition, with its origin. Count the per-collection daily bound from that history, excluding undos and field additions.
- [x] 3.3 Show the 5 newest entries and the total per collection in the Records and Planning inventories. Page the full history through `inspect`.
- [x] 3.4 Test each refused bound once, a committed automatic restructure on each storage mode, an exempt undo, and the inventory's bounded history.

## 4. Planning

- [x] 4.1 Refuse field steps on Planning core fields.
- [x] 4.2 Keep item identity in a merge or split, carry same-collection `parent` and `area` references on a merge, and refuse a split that separates an item from them. A merge's field mapping calls the core-field guard of 4.1; a split keeps every field, so it changes no core field.
- [x] 4.3 Test a refused core-field step on a file-only and a store Planning collection.
- [x] 4.4 Test a merge that keeps references resolvable and a refused orphaning split.

## 5. Envelope

- [x] 5.1 In `src/exomem/envelope.py`, serve a bounded automatic restructure under `proactive_capture` and keep the rest, and every permanent deletion, under `restructure_execution`. Update the served texts that say schema changes and merges stay confirm-required.
- [x] 5.2 Update the envelope tests that pin the old split.

## 6. What reaches the user

- [x] 6.1 In `mutation_terminal.py`, name a Records or Planning row by its label and its collection by its title in `user_summary`, for file-backed and SQLite-backed writes. Release the label only under the existing title check. Keep the stored terminal unchanged.
- [x] 6.2 Define the restructure summary per step kind once, beside the step definitions. Give renames, retypes and rekeys no `user_summary`.
- [x] 6.3 Extend `NARRATION_RULE` with the plain-words and no-mention sentences. Measure the compact bootstrap ceiling before and after, and trim to fit without raising it.
- [x] 6.4 Replace "name the collection and the rows" in the served Records procedure with "report what you kept, in the user's words". Teach the restructure, the surface-or-silent rule, the one-question rule and explicit design. Cover the scaffold (`SKILL.md`, `references/planning-records.md`, `references/operations.md`), the bootstrap Records teaching in `commands.py`, and the `add_record` and `change_records` descriptions in `operation_entries.py`.
- [x] 6.5 In `_hooks/exomem_capture_nudge.py`, drop the `Saved ->` reply match and count a save from the tool result alone. Replay the recorded reminder corpus and report the reminder rate before and after. Applied: `python -m membench.outcome.incidents.reminder_replay` replayed the 120 hooked Stops of the incident round of 2026-11-23 (40 cases, both arms) through the hook on main and this change's. With the transcript readable, as in an interactive session, neither reminded. With it missing, this change's hook reminded 3 times and main's none, each at the maximal level after a save that the hook could not see; the harness ran a one-turn session unpersisted, and now keeps a hooked session's transcript.
- [x] 6.6 Test that a row append's `user_summary` holds no path on each storage mode. Test that a withheld key names the collection alone, and that a rekey has no summary.
- [x] 6.7 Regenerate the skill-contract stamps, the plugin skills, the cloud plugin bundles and the tool-surface fingerprint. Run each generator's own check.
- [x] 6.8 Carry the reshape on the surfaces a session reads without bootstrap (design decision 4): a reshape clause in the per-turn capture rules within the hook's 1,000-character cap, `reshape_route` on the Records and Planning inventory, the rekey remedy on an `add_record` whose derived key a stored row holds, and `origin: user_design` on `new_collection`. Name an Entity merge in the instruction blocks' confirmed merge rule. Applied: `restructure.RESHAPE_CUE` closes `prominence._CAPTURE_RULES` 5 characters under the cap; `record_governance` returns `reshape_route` on the inventory; `record_memory._KEY_TAKEN_REMEDY` names the rekey step; `new_collection` describes `origin: user_design`; the instruction blocks in `docs/prominence.md` keep confirmation for "Entity merge or page curation".

## 7. Latency proof

- [x] 7.1 Measure the hold of a restructure at the 256-row bound for each step kind and its undo, on an enrolled store through the service and on a store without a marker, with three repeats. Each hold stays at most 0.5 s. Measured 2026-11-23 on a collection of a few hundred rows, on a laptop at load 13-14, three repeats each: the longest hold was 0.321 s on the store without a marker and 0.319 s through the service, both a 128-row split. At 384 rewritten rows the service path held a merge for 0.576 s, which set the 256-row bound. Those holds grow with the rows of the collection, not only with the rows written, so the collection bound comes from `scripts/measure-restructure-hold.py ROWS --repeats 3`, measured 2026-11-24 on a store without a marker at load 3.6-3.9, with each of retitle, add field, rename, retype, split, merge and rekey run with its undo: the worst hold was 0.363 s at 4,000 rows, 0.454 s at 6,000 rows (a split) and 0.543 s at 8,000 rows. The bound is 4,000 current rows: the service path at that size is not measured, and at 384 rewritten rows a merge held about a fifth longer through the service than on a store without a marker, which would take 6,000 past the budget.

## 8. Acceptance on real models

- [x] 8.1 Add the `restructure`, `rows_preserved` and `plain_reply` checks to `benchmarks/membench/outcome/incidents/`.
- [x] 8.2 Add the cases that design.md lists, in invented domains, for the hooked and the hookless actor.
- [ ] 8.3 Run the parent change's head against this change at one draw. Read every failure's trace and fix its cause. Hand over the passing cases at three draws. State the hookless actor's cost before the paid run and record each paid run in the eval-runs collection.
- [x] 8.4 If replies still name internals once the reshape works (8.3), carry the plain-words sentence on the write receipt that the reply is composed from. The custom-instruction blocks and the per-turn capture rules have no room under their caps (design decision 6). Applied: in rounds incidents-ams-r1 and r2 the replies named collections, fields, keys and rows in 6 of 7 failing structure cases, so every Records and Planning write reply carries `say` (`mutation_terminal.PLAIN_REPLY`), and the key-taken refusal ends with the same instruction.

## 9. Refresh after capture-structured-work-automatically archives

Each item below MODIFIES a requirement that exists only in that change. Write each delta after it archives, copying the then-canonical text and keeping every scenario.

- [ ] 9.1 `delegation-envelope` "Collection creation is bounded capture or confirmed restructuring": a bounded lossless restructure follows `proactive_capture`; drop schema changes, merges and migrations from its `restructure_execution` list and from its standing-delegation scenario.
- [ ] 9.2 `delegation-envelope` "Handing over an artifact does not bypass write authority": a schema change follows the restructure classification, not a separate confirmed `restructure_execution`.
- [ ] 9.3 `records` "Agents infer Records participation from observed state": report in the user's words, and keep a further occurrence as its own row instead of updating the row that the key names.
- [ ] 9.4 `records` "An automatic collection creation is bounded by the server": the inventory's automatic flag becomes the shape-history origin.
- [ ] 9.5 `records` "`add_record` creates a minimal collection for its first row": the receipt states the creation in plain words, a retired title is refused with a pointer to `restore`, and `origin: user_design` records a create whose details the user named, which no automatic-creation bound limits.
- [ ] 9.6 `agent-bootstrap-contract` "Bootstrap teaches Records routing and boundaries" and "The capture predicate covers stated intent and observed outcomes": report in the user's words, and teach the restructure and the one-question rule.
- [ ] 9.7 `records-release-acceptance` "Codex and Claude infer Records without prompt naming": the report names the user's things in their words.

## 10. Fold the receipt precedence

- [ ] 10.1 After `accelerate-durable-write-acknowledgement`, `return-minimal-agent-replies` and `replace-agent-tool-surface` archive, fold "A Records or Planning receipt names the user's things" into "Compact Success Is The Default Projection". Remove its precedence sentence. Refresh the narration MODIFIED block if `replace-agent-tool-surface` archives first.

## 11. Delivery

- [ ] 11.1 Run the scoped suites while building and the full corpus before review. Run the privacy gate.
- [ ] 11.2 Deliver in one PR with an independent review. The change touches stored data, so one review pass applies unless the reviewer writes `Risk lens: yes`.
- [ ] 11.3 After release, run the acceptance check on the installed personal service from a fresh session.
- [ ] 11.4 Synchronise the deltas into the canonical specs and archive with `openspec archive`, validating before and after.

## Follow-ups (separate changes)

- Graph restructuring: entity merge, split and retype, and relation retype, under the same principles. It builds on `promote-referents-without-nudges` and the curation compensation plan.
- Automatic creation of a Planning collection.
- Restructures of more than 256 rewritten rows, or of a collection of more than 4,000 rows, as a resumable job: reads serve the prior shape until publish, and writes to that collection refuse `COLLECTION_RESTRUCTURING` with a retry hint. It needs the store changes that design decision 8 names. Measured on 2026-11-23 at 100,000 rows: a set-based publish of every new version took 1.5-2.9 s, and moving the current-version pointers alone took 0.3-0.95 s, against a 0.5 s hold budget.
