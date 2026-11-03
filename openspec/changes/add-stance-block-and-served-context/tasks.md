Step 1 decided the gates of the first draft (proposal.md, "Gate"), so no task here is gated. Opening this change waits for the live install and the import fix (feature freeze).

## 1. Page and unit keys

- [ ] 1.1 Unit tests first for every scenario of the semantic-unit-language delta: strict `as_of` and `until` dates, `invalid_rich_date_order`, and metadata-only units reported as empty.
- [ ] 1.2 Parse and project `as_of` and `until` onto the parsed unit and its serialized form, and add them to the reserved metadata rows.
- [ ] 1.3 Tests first, then parse the page frontmatter key `stance`: a governed write refuses a value other than `true` or `false` with an error that names the key and both values; a hand edit with such a value gets a validation finding and is not a stance page.
  Proof: the parser and writer tests pass, the existing semantic-unit tests show no new failure against the base commit, and write validation rejects every `stance` value that commit rejects.

## 2. Typed currency

- [ ] 2.1 Unit tests first for the rule order: superseded or archived wins over every date; past `until` is history with no write and a live lifecycle; past `check_by` stays current with `review_due`; no date stays current; `as_of` replaces the prose date as `updated`; today is the due-state projection's day.
- [ ] 2.2 Apply the rules in the packet builder's currency step to every unit it serves.
  Proof: the currency tests pass, and the activation benchmark's negative controls show no change, because no existing unit carries the new keys.

## 3. Stance block

- [ ] 3.1 Unit tests first: every unit of a stance page is a line; path order, then unit order; whole lines under `STANCE_MAX_CHARS` with an `omitted` count; `ready`, `empty` and `unavailable`; a line past `until` withheld and counted in `expired`; a line past `check_by` served with `review_due`; a superseded line never served and counted nowhere; a withheld page absent from every count.
- [ ] 3.2 Unit test: the cache key covers the caller's view, the stance input generation and the next date boundary; a line with `until: 2026-12-01` cached on 2026-11-30 misses the cache on 2026-12-02; a request with a conversation reuses the cached block.
- [ ] 3.3 Serve the block in every maps packet, with `STANCE_MAX_CHARS` (800) as a named constant that carries a one-line reason, and charge only the used characters to `max_chars` before the map.
- [ ] 3.4 Wire `EXOMEM_ACTIVATION_STANCE` (default `on`); `off` removes the block and charges nothing for it.
  Proof: a fixture vault with no stance page gets `stance.state = "empty"` and the same map as before, less the block's keys; a fixture with stance pages gets the expected lines and counts.

## 4. Map rename and the empty-slice sentence

- [ ] 4.1 Rename the map item field `activity` to `updated` in the catalogue projection, the context views, `exomem://map` and the retrieve hook; name the rename in the release notes as a breaking client change.
- [ ] 4.2 Give an `empty` slice the description that the map and the slice cover compiled material only, that `search_memory` reaches captured Sources, and that an empty slice is not proof of absence, with the call rendered by `capabilities.active_call_text`.
  Proof: a maps packet carries `updated` and no `activity` on every map item, and an empty slice carries the sentence.

## 5. Served-context record

- [ ] 5.1 Unit tests first: one row per maps activation with the listed fields and no text column; stance line refs and slice refs with their match; the `next_action` state when present; surfaced upkeep items by queue item id; append-only; the retention sweep by age and by row cap; rows recorded under the caller's principal and view.
- [ ] 5.2 Create the table in the vault state directory beside heat; write each row after the packet is built with a short busy timeout; never fail, retry or wait on the write.
- [ ] 5.3 Add `served_context_id` after the packet cache lookup; on a failed write, omit it and name `served_context_unrecorded` in diagnostics; test that a cached packet gets a fresh id and stores none.
- [ ] 5.4 Add the owner read-out on the CLI and the owner's REST facade over one leaf: served, opened and cited counts of slice refs with rows and sessions, joined with heat by session key and time, both windows reported, an uncovered count reported as unknown, and one row shown by id.
- [ ] 5.5 Test that no nonowner surface returns rows, and that no host log, timing record or activation log line holds a row field.
  Proof: the record tests pass, and an activation with a locked record database is served unchanged.

## 6. `next_action`

- [ ] 6.1 Unit tests first for the state table: the precedence `ambiguous`, `review_due`, `slice_empty`, and `search_miss` for search; one move per response; `say` at most 240 characters; `tool` only for a one-call move; `args` only when complete; every named call rendered by `capabilities.active_call_text`; no field when no state applies; no branch on a client name.
- [ ] 6.2 Write the moves: `ambiguous` (moved from the hook), `review_due`, `slice_empty`, and `search_miss` carrying the bootstrap `miss_rule`, which leaves bootstrap.
- [ ] 6.3 Add `next_action` to `recall_context` and `search_memory` after the packet cache lookup; reserve 240 characters of `max_chars` in every maps packet; when the table fails, omit the field and name `next_action_unavailable` in diagnostics.
- [ ] 6.4 Replace the per-state moves in the server instructions, both tool descriptions and the skill recall loop with "Follow `next_action` when present."; keep the server instructions within 900 characters.
  Proof: the state-table tests pass, and the per-state move text exists once in `src/exomem`.

## 7. Retrieve hook

- [ ] 7.1 Tests first: the stance block renders once per stance generation in a session, then one pointer line, then again on a changed generation; a `say` line renders at its fixed position; the hook holds no per-state line of its own; the render ceiling and the failure fallback hold.
- [ ] 7.2 Implement the rendering.
  Proof: the hook tests pass, and the existing working-set hook tests show no new failure against the base commit.

## 8. Stance-line route and bootstrap

- [ ] 8.1 Teach the stance-line route in both `SKILL.md` copies, `prominence.py`, the scaffold recall loop and the bootstrap `intent_boundary` and `capture_examples`: model judgment of a correction for every task, propose the exact line with the user's words as its source, write it on a stance page after confirmation, `supersedes` for a later line, no stance line for one kind of task or one entity, write only when asked at `light`, propose only at `off`.
- [ ] 8.2 Change the bootstrap activation line to "Before a substantive turn with no prior context, call `recall_context` with the turn verbatim. Follow `next_action` when present.", byte-identical with the scaffold recall loop and within 220 bytes.
- [ ] 8.3 Test the compact bootstrap ceiling at every prominence level and surface, with the 512-byte margin at balanced, and run the scaffold leak guard.
  Proof: the bootstrap and scaffold tests pass.

## 9. Incidents

- [ ] 9.1 Teach the context-miss-learning workflow to link the delivered context by `served_context_id`, and to record an absent or expired row as a gap.
  Proof: task 11.3.

## 10. Generated artifacts

- [ ] 10.1 Regenerate `docs/capabilities.md`, the plugin tree, the hosted render and every other derived copy of the tool descriptions and the skill scaffold; add new test modules that import `benchmarks/` to `tests/harness_modules.txt`.
  Proof: the generated-artifact checks pass in CI.

## 11. Acceptance

- [ ] 11.1 Measure activation p95 on the activation benchmark with `include_timings`, before and after, on the same fixture; report the stance, record and `next_action` stage times.
- [ ] 11.2 Ground-truth check of the record: on the first development run of the built change on the step-1 harness, compare each case's recorded stance line refs, slice refs, `next_action` state, opens and cites with the responses and tool calls in the harness transcript. It passes with zero disagreements across the development cases.
- [ ] 11.3 Replay one incident shape through the context-miss-learning workflow and check that the incident links its delivered context by `served_context_id`.
- [ ] 11.4 Prove the client-dependent behaviour on real models on the step-1 harness: the required items used against the default map; whether the agent follows each move (`ambiguous`, `review_due`, `slice_empty`, `search_miss`); and whether it proposes a stance line and waits for confirmation, including a user who declines. Use realistic and hostile inputs, and inject a provider outage at the transport.
- [ ] 11.5 Run the full test corpus in CI-shaped shards, and get an independent adversarial review of the diff with a recheck of each correction.

## 12. Closure

- [ ] 12.1 Before and after the archive, run `openspec validate --all --strict`.
- [ ] 12.2 After merge, sync these deltas into the canonical specs, refresh the text of any unarchived change that this change widened (design D5 and D9), and archive the change with `openspec archive`.
