Gate markers: a group or task marked `Gate: step 1 —` is built in full but kept or cut by its decision rule once the step-1 results land. A group or task without a marker is not gated. Opening this change waits for the live install and the import fix (feature freeze).

## 1. Registries

- [ ] 1.1 Add the `activities` registry on the shared registry contract: an empty shipped pack `src/exomem/vocabulary/packs/core/activities.yaml`, the overlay `_Schema/activities.yaml`, the `schema_memory(subject="activities")` subject, the key grammar from workflow-contract activity selectors, a required one-line `description` of at most 160 characters, and alias and deprecation resolution.
- [ ] 1.2 Register a default-deny egress path for the activities overlay, so an unreleased overlay is absent for the caller.
- [ ] 1.3 Add the `guidance-roles` registry with a shipped pack of `method` (family `method`) and `precedent` and `failure` (family `case`), an overlay, and a validator that rejects an entry without a `family` or with a family outside `method` and `case`.
- [ ] 1.4 Unit tests first: a new vault inspects to zero activities; an owner save takes effect at once and a nonowner save waits as pending; a restricted caller sees no registry and its declared keys count as unknown; an overlay role with family `method` groups as a method.
  Proof: the registry tests pass, and a code search finds no activity or role key in `src/exomem` outside the packs.

## 2. Unit metadata keys

- [ ] 2.1 Unit tests first for every scenario of the semantic-unit-language delta: strict `as_of` and `until` dates, `invalid_rich_date_order`, the `activities`, `guidance` and `guidance_origin` errors, the `unregistered_activity` and `unregistered_guidance_role` findings, and metadata-only guidance units reported as empty.
- [ ] 2.2 Parse and project the five keys onto the parsed unit and its serialized form, and add them to the reserved metadata rows.
- [ ] 2.3 Parse the page frontmatter keys `activities`, `guidance`, `stance` and `guidance_origin` with the same rules.
  Proof: the parser tests pass, and the existing semantic-unit tests show no new failure against the base commit.

## 3. Declarations and origin

- [ ] 3.1 Unit tests first: a delegated agent's declaration records `guidance_origin: inferred`; the owner principal with the owner's confirmation records `reviewed`; a nonowner principal cannot record `reviewed`; a hand-written declaration with no origin reads as `authored`; a governed write that adds a line to a stance page records the page origin again.
- [ ] 3.2 Stamp `guidance_origin` in every governed writer that adds or changes `activities`, `guidance`, `stance` or an `entity`-family relation on a page or rich unit.
- [ ] 3.3 Refuse a governed write whose `activities` holds a key absent from the caller's view of the registry, with an error that names `schema_memory(subject="activities")`; report a validation finding for the same key in a hand edit.
  Proof: the writer tests pass, and write validation rejects every declaration that commit rejects.

## 4. Typed currency

- [ ] 4.1 Unit tests first for the rule order: superseded or archived wins over every date; past `until` is history with no write and a live lifecycle; past `check_by` stays current with `review_due`; no date stays current; `as_of` replaces the prose date as `updated`; today is the due-state projection's day.
- [ ] 4.2 Apply the rules in the packet builder's currency step to every unit it serves.
  Proof: the currency tests pass, and the activation benchmark's negative controls show no change, because no existing unit carries the new keys.

## 5. Scope rule and selection — Gate: step 1 — the whole task layer ships only if the stub arm beats the ablation arm and the stance-only arm

- [ ] 5.1 Build the declaration index from units and index rows, never from page text at read time: item ref, activities, role, origin, `entity`-family targets, `as_of`, `until`, `check_by`.
- [ ] 5.2 Unit test: a page that supersedes its own method unit for `review-contract` never yields the superseded unit as guidance.
- [ ] 5.3 Unit test: with person B declared, selection returns no unit scoped to person A.
- [ ] 5.4 Unit test: for a restricted caller who cannot see an item, or cannot see its target entity, the guidance state equals the state of a vault with no such item.
- [ ] 5.5 Unit tests: an activity-only declaration does not unlock an entity-scoped item; an item with no scope and no stance mark is not guidance; an `inferred` item counts as absent; code reads entity scope only through the relation family `entity` and the anchor kind `entity`.
- [ ] 5.6 Implement selection over the caller's view to pass 5.2–5.5.
  Proof: the selection tests pass, and a code search finds no branch on a relation key, an entity-type key or `cue_nouns` in the new code.

## 6. Guidance block and states — Gate: step 1 — the whole task layer (rule as in group 5)

- [ ] 6.1 Unit tests first: the closed state order `selected`, `none_reviewed`, `unknown_activity`, `none_declared`, and `unavailable` on a failed lookup; item order by scope, then family, then ref; whole items only with an `omitted` count.
- [ ] 6.2 Serve the `guidance` block with `{ref, title, role, why}` and its fixed `description`, charged first to the slice reserve; slice items fill the rest; the map budget stays unchanged.
- [ ] 6.3 Wire `EXOMEM_ACTIVATION_GUIDANCE` (default `on`); `off` removes the `stance` and `guidance` blocks and ignores the declarations.
  Proof: a maps packet for a fixture vault serves the expected guidance refs and states, and its map items equal those of the same request without a declaration.

## 7. Declared activity and entities

- [ ] 7.1 Gate: step 1 — the whole task layer (rule as in group 5). Unit tests first for `conversation.activities`: deduplication, the four-key bound, malformed, surplus, unregistered, withheld and deprecated keys counted in `unknown_activities`, aliases resolved, no unknown key echoed, `generation.conversation` unchanged, ignored in anchors mode.
- [ ] 7.2 Gate: step 1 — the whole task layer (rule as in group 5). Accept `activities` on MCP, the CLI (`--activity`) and the REST facade over one leaf, and keep the declaration out of the continuity token.
- [ ] 7.3 Gate: step 1 — `conversation.entities` and the `declared` match stay only if E0 leaks on B's task while E1 still applies on A's; otherwise move this task to "Next". Accept `conversation.entities` (`--entity`), resolve each ref to an anchor of kind `entity` in the caller's view, count drops in `unknown_entities`, list a resolved entity first in the slice with match `declared`, and list its `profile` ref first in the guidance block.
- [ ] 7.4 Test the ephemeral rule: an unresolved activity key leaves no trace in any file under the machine-local state root, and only its count reaches the record.
  Proof: the conversation tests pass, and the existing conversation-aware activation tests show no new failure against the base commit.

## 8. Stance block

- [ ] 8.1 Unit tests first: unscoped lines only; scoped stance pages and units become guidance items; path order then unit order; two caps with `omitted` counts; `ready`, `empty` and `unavailable`; a line past `until` withheld and counted in `lines.expired`; a line past `check_by` served with `review_due`; computed over the caller's view.
- [ ] 8.2 Unit test: the cache key covers the caller's view, the registry hash, the stance input generation and the next date boundary; a line with `until: 2026-12-01` cached on 2026-11-30 misses the cache on 2026-12-02.
- [ ] 8.3 Serve the block in every maps packet, with `STANCE_LINES_MAX_CHARS` (800) and `STANCE_INDEX_MAX_CHARS` (400) as named constants that each carry a one-line reason, and charge only the used characters to `max_chars` before the map. Gate: step 1 — the whole task layer (rule as in group 5) for the activity index part.
  Proof: a fixture vault with no stance page gets `stance.state = "empty"` and the same map as before; a fixture with stance pages gets the expected lines and counts.

## 9. Map rename and the empty-slice sentence

- [ ] 9.1 Rename the map item field `activity` to `updated` in the catalogue projection, the context views, `exomem://map` and the retrieve hook; name the rename in the release notes as a breaking client change.
- [ ] 9.2 Give an `empty` slice the description that the map and the slice cover compiled material only, that `search_memory` reaches captured Sources, and that an empty slice is not proof of absence, with the call rendered by `capabilities.active_call_text`.
  Proof: a maps packet carries `updated` and no `activity` on every map item, and an empty slice carries the sentence.

## 10. Served-context record

- [ ] 10.1 Unit tests first: one row per maps activation with the listed fields and no text column; unknown keys as counts only; append-only; the retention sweep by age and by row cap; rows recorded under the caller's principal and view.
- [ ] 10.2 Create the table in the vault state directory beside heat; write each row after the packet is built with a short busy timeout; never fail, retry or wait on the write.
- [ ] 10.3 Add `served_context_id` after the packet cache lookup; on a failed write omit it and name `served_context_unrecorded` in diagnostics; test that a cached packet gets a fresh id and stores none.
- [ ] 10.4 Add the owner read-out on the CLI and the owner's REST facade over one leaf: per-activity served, opened and cited counts with rows and sessions, joined with heat by session key and time, both windows reported, an uncovered count reported as unknown, and one row shown by id.
- [ ] 10.5 Test that no nonowner surface returns rows, and that no host log, timing record or activation log line holds a row field.
  Proof: the record tests pass, and an activation with a locked record database is served unchanged.

## 11. `next_action` — Gate: step 1 — `next_action` stays if the arms with it raise the declare rate or the guidance-read rate over the arms without it; otherwise the per-state sentences move into the tool description, the server instructions and the skill recall loop, and the hook keeps its own lines

- [ ] 11.1 Unit tests first for the state table: the fixed precedence; one move per response; `say` at most 240 characters; `tool` only for a one-call move; `args` only when complete; every named call rendered by `capabilities.active_call_text`; no field when no state applies; no branch on a client name.
- [ ] 11.2 Write the moves: `ambiguous` (moved from the hook), `guidance_selected`, `guidance_unknown_activity`, `guidance_none_declared` (maps never pre-select task context; declare the activity from the index), `review_due`, `slice_empty`, and `search_miss` carrying the bootstrap `miss_rule`, which leaves bootstrap.
- [ ] 11.3 Gate: step 1 — the check sentence stays if "Stub + check" beats Stub; otherwise `guidance_selected` keeps only "read the named guidance before a substantive conclusion" and "name the guidance that shaped the answer". Put the check sentence in `guidance_selected`: before a substantive conclusion, check the draft against each served guidance item, then state in one line which items the answer applied or set aside, by ref.
- [ ] 11.4 Gate: step 1 — the correction route (rule as in task 13.1). Accept `refresh_reason` (`new_evidence`, `task_change`, `correction`) on `task_refresh` on every door; ignore an unknown or misplaced value and name `refresh_reason_ignored` in diagnostics; add the `correction` move.
- [ ] 11.5 Add `next_action` to `recall_context` and `search_memory` after the packet cache lookup; reserve 240 characters of `max_chars` in every maps packet; when the table fails, omit the field and name `next_action_unavailable` in diagnostics.
- [ ] 11.6 Replace the per-state moves in the server instructions, both tool descriptions and the skill recall loop with "Follow `next_action` when present."; keep the server instructions within 900 characters.
  Proof: the state-table tests pass, and the per-state move text exists once in `src/exomem`.

## 12. Retrieve hook

- [ ] 12.1 Tests first: the stance block renders once per stance generation in a session, then one pointer line, then again on a changed generation; a `say` line renders at its fixed position; the hook holds no per-state line of its own; the render ceiling and the failure fallback hold.
- [ ] 12.2 Implement the rendering. Gate: step 1 — `next_action` (rule as in group 11) for the `say` line and the removal of the hook's own lines.
  Proof: the hook tests pass, and the existing working-set hook tests show no new failure against the base commit.

## 13. Correction route and bootstrap

- [ ] 13.1 Gate: step 1 — the correction route stays if C1 beats C0 and R1 beats R0 in most repeats with the control clean; otherwise it shrinks to skill text and task 11.4 is cut. Teach the correction route in both `SKILL.md` copies, `prominence.py`, the scaffold recall loop and the bootstrap `intent_boundary` and `capture_examples`: model judgment of the kind, propose the exact unit with the user's words as its source, write after confirmation with `guidance_origin: reviewed`, refresh with `refresh_reason = "correction"`, `supersedes` and `contradicts` for conflicts, write only when asked at `light`, propose only at `off`.
- [ ] 13.2 Gate: step 1 — `next_action` (rule as in group 11). Change the bootstrap activation line to "Before a substantive turn with no prior context, call `recall_context` with the turn verbatim. Follow `next_action` when present.", byte-identical with the scaffold recall loop and within 220 bytes.
- [ ] 13.3 Test the compact bootstrap ceiling at every prominence level and surface, with the 512-byte margin at balanced, and run the scaffold leak guard.
  Proof: the bootstrap and scaffold tests pass.

## 14. Workflow contracts and incidents

- [ ] 14.1 Tests first: a contract save refuses an activity selector that is not a current registry key and names the canonical key for an alias; a stored contract with such a selector keeps resolving and carries `unregistered_activity`.
- [ ] 14.2 Implement the save validation and the read warning.
- [ ] 14.3 Teach the context-miss-learning workflow to link the delivered context by `served_context_id`, and to record an absent or expired row as a gap.
  Proof: the workflow-contract tests pass, and the existing resolution tests show no new failure against the base commit.

## 15. Generated artifacts

- [ ] 15.1 Regenerate `docs/capabilities.md`, the plugin tree, the hosted render and every other derived copy of the tool descriptions and the skill scaffold; add new test modules that import `benchmarks/` to `tests/harness_modules.txt`.
  Proof: the generated-artifact checks pass in CI.

## 16. Acceptance

- [ ] 16.1 Measure activation p95 on the activation benchmark with `include_timings`, before and after, on the same fixture; report the stance, guidance, record and `next_action` stage times.
- [ ] 16.2 Ground-truth check of the record: on the first development run of the built change on the step-1 harness (step 2), compare each case's recorded guidance refs, opens and cites with the tool calls in the harness transcript. It passes with zero disagreements across the development cases.
- [ ] 16.3 Replay one incident shape through the context-miss-learning workflow and check that the incident links its delivered context by `served_context_id`.
- [ ] 16.4 Prove the client-dependent behaviour on real models on the step-1 harness (declare rate, guidance-read rate, required items used), with realistic and hostile inputs and a provider outage injected at the transport.
- [ ] 16.5 Run the full test corpus in CI-shaped shards, and get an independent adversarial review of the diff with a recheck of each correction.

## 17. Closure

- [ ] 17.1 Before and after the archive, run `openspec validate --all --strict`.
- [ ] 17.2 After merge, sync these deltas into the canonical specs, refresh the text of any unarchived change that this change widened (design D18), and archive the change with `openspec archive`.
