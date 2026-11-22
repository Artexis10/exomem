# Tasks

## 1. Envelope authority

- [x] 1.1 In `src/exomem/envelope.py`, serve collection creation within the automatic-creation bounds under `proactive_capture`, and keep `restructure_execution` fixed for every other creation and for schema changes, merges, deletions and migrations. Replace the founder-gate and confirm-required texts that say automatic collections stay refused.
- [x] 1.2 Update the envelope, promotion-class, personal-baseline and collection-claims tests that pin the old refusal so they pin the new split.

## 2. The server bound on an automatic creation

- [x] 2.1 Add `automatic` to `change_records` `create`. With it, refuse `AUTOMATIC_CREATION_REFUSED` unless every bound in the records requirement holds. Define the bounds once, beside the check, and let them hold on a file-only vault and a store vault. Count the last 24 hours from each storage mode's own create record: `collections.created_txn` on a store vault, a marker line in the create's activity-log entry on a file-only vault.
- [x] 2.2 Record `automatic: true` on the create's receipt and, on a file-only vault, in its activity-log entry. Report per collection in the inventory whether it was created automatically.
- [x] 2.3 Add one test for each refused bound (disposition, schema, daily count on a store vault and on a file-only vault). Add one test that a minimal automatic creation commits and is reported in the inventory, on a store vault and on a file-only vault. Add one test that the same manifest without the flag creates as before.

- [x] 2.4 Add `new_collection` to `add_record`: when `collection` is a title that names no collection, draft the minimal Records manifest from the row, create it through the automatic create and append the row, as one call with one receipt. A refusal creates nothing and returns the confirmed create call with the manifest. Test the call on a file-only vault and on a store vault, the advisory refusal, an existing title, and a repeated call.

- [x] 2.5 Add `new_fields` to `add_record` and `edit_record`: under silent `proactive_capture` the server adds the fields the call names, typed from the values, through the manifest revision, then writes the row, as one call with one receipt that names the added fields. Refuse under `advisory`, `off`, past 12 fields for a collection that was created automatically (the create record tells; any other collection takes any number of fields), for a field declared with another type and for a Planning collection. Make the `SCHEMA_UNKNOWN_FIELD` refusal carry the retry call or the proposal. Test it on a file-only vault and on a served store vault: silent capture, advisory refusal, the retry, the field bound, and a replay.

## 3. Served contract

- [x] 3.1 In `src/exomem/prominence.py`, widen the correction sentence to cover a decision or standing rule agreed in conversation, and add the structured-work cue to the activation `capture_policy` block's per-turn `rules` key. Serve the sentence in `balanced`, `maximal` and the block. Measure the compact bootstrap ceiling and the custom-instruction byte caps before and after.
- [x] 3.2 Replace the refusal in the bootstrap Records teaching and route safety text (`commands.py`), the `collection_candidate` guidance (`audit.py`) and the `change_records` description (`operation_entries.py`).
- [x] 3.3 In the scaffold under `src/exomem/_scaffold/_Schema/`, replace "manual-first" and "must not silently create a long-lived schema" with the recognise, discover, reuse or create, backfill and report procedure. Cover `references/operations.md`, `references/planning-records.md`, `SKILL.md` and the workflow skills. Teach separate attribution and unresolved identities.
- [x] 3.4 In `references/mutation-results.md` and the capture skill, state the recovery recipe for `MUTATION_ACKNOWLEDGEMENT_PENDING`. In the bulk contract (`record_memory.py`), state that `source` is required, once for the batch or on each row.
- [x] 3.5 Update the hand-written guides that repeat the old rule: `docs/ai-assistant-guide.md`, `QUICKSTART.md`, `docs/prominence.md`, `docs/records.md` and `docs/product-model.md`.
- [x] 3.6 Regenerate the skill-contract stamps, the Claude Code plugin skills, the cloud plugin bundles, and the tool-surface fingerprint with its pending ChatGPT digest. Run each generator's own check.

## 4. Acceptance on real models

- [x] 4.1 Extend `benchmarks/membench/outcome/incidents/`. Add cases with several user turns in one session, a follow-up fresh session whose answer a blind judge checks, and `new_collection` and `no_new_collection` checks.
- [x] 4.2 Add the cases that design.md lists, in invented domains, for the Claude Code actor and the hookless OpenAI actor.
- [x] 4.3 Run base against changed at one draw. Read every failure's trace and fix its cause. Hand over the passing cases at three draws, reported as right on every draw. State the OpenAI cost before the paid run, and record each paid run in the eval-runs collection.
  - Evidence, 2026-11-22: every run is recorded in the eval-runs collection. The OpenAI actor did not run, because the evaluation's OpenAI credits were exhausted; by the owner's direction, a hookless Claude Code actor (every hook disabled, on the subscription) stood in for the client without hooks.
  - Hooked actor, one draw, all 12 cases: changed 10 of 12. Hookless actor, one draw: base 8 of 12, changed 9 of 12; changed gains the equipment-maintenance and ambiguous-identity cases. The hookless capture-off guard passed 3 of 3 per arm on a rerun after one changed-arm miss.
  - Hand-over, hooked actor, three draws: right on every draw for reuse (file and store), correcting a stored value, ambiguous identity, a passing mention, capture off, an agreed decision and an interrupted write (file and store). Equipment maintenance was right on 2 of 3.
  - Not passing: the two evaluation cases. Agents now create the collection and, with `new_fields`, often give an outside claim its own field, but they key a recurring item on the item alone. Four rounds of creation-time wording left one row per dated trial at 0 to 1 of 4. The durable fix is automatic re-keying when the second trial arrives, which `let-agents-manage-structure` specifies.
- [x] 4.4 Only if the hooked arm still misses agreed decisions after 4.3, widen the client capture reminder's trigger as design.md decision 4 describes. Keep it only if an ablation shows it pays.
  - Not needed: the agreed-decision case was right on 3 of 3 hooked draws and on both hookless arms, so the reminder's trigger stays as it is.
- [ ] 4.5 Run the Codex arm of the release acceptance (the client without hooks, on its own model) when its quota returns, on the same cases at one draw against base, and record it.

## 5. Delivery

- [ ] 5.1 Run the scoped suites while building and the full corpus before review. Deliver in one PR with an independent review.
- [ ] 5.2 After release, run the acceptance check on the installed personal service from a fresh session.
