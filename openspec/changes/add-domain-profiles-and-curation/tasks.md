# Tasks

## 1. Settle placement with the layout owner

- [x] 1.1 Confirm the profile area with the single-store layout decision; record the shipped `profile` type, its folder and domain partition.
  - The layout owner confirmed that the root-native layout is group 9 of `make-vault-store-canonical`. Until a relayout runs, new pages use the current layout, and compiled areas follow the note-type registry folder at relayout.
  - The owner chose one profile area with no domain partition: folder `Notes/Profiles`, with the domain in `topics` (design D1).

## 2. Domain profiles

- [x] 2.1 Add the `profile` note type and the domain `parent` field. Verify placement of a global and a domain profile, rejection of a two-topic profile, and rejection of an unknown parent and a cycle.
  - `tests/test_domain_profiles.py` covers both placements, the two-topic refusal at preview and commit, and an unknown or cyclic parent.
- [x] 2.2 Run round R1 on a realistic fixture with keys written from the turn text, and record the selection arm in design D6.
  - Arm (c) won on the test half, with intervals that exclude zero against both other arms. Design D6 records the result and the shipped rule.
- [x] 2.3 Serve `domain_profiles` with the selected arm, inheritance, both budgets, the off switch, egress and the terminal guard. Verify the gaming, two-domain, general-knowledge and over-budget scenarios, and terminal egress on a release-gated vault.
  - `tests/test_domain_profiles.py` covers the gaming, two-domain, general, over-budget and hidden-page cases; the profile keyword reading runs at the activation's pinned lexical checkpoint and walks no vault files.
- [x] 2.4 Carry profile generations in continuity, and render profiles in the hook. Verify a later same-domain turn and an edited profile, and add one hook test on a real service packet.
  - `tests/test_domain_profiles.py` covers a held profile, an edited profile, malformed continuity, the 16-entry token cap and the hook rendering on a served packet.
- [x] 2.5 Supply stance from a profile page with no topics. Verify that a retyped global page serves the same lines.
  - `test_a_retyped_global_profile_serves_the_same_stance_lines`.

## 3. Profile curation

- [x] 3.1 Add the `upkeep_profile_curation` family. Verify detection of new units, a vault-mapped category, supersession, contradiction, expiry and `check_by`, all without a model.
  - `tests/test_dreamer_profile_curation.py` covers each condition, a vault-mapped and an unmapped category, linked pages and a global profile.
- [x] 3.2 Surface due items through upkeep delivery, and verify that a dismissal holds until the evidence changes.
  - `test_a_dismissed_item_stays_quiet_until_new_evidence` and `test_the_agent_is_served_the_profile_its_evidence_and_the_edit_route`.
- [x] 3.3 Teach seeding and curation in the generic scaffold: the agent proposes exact lines, the owner confirms, and a vault maps its own preference categories to roles.
  - The scaffold's `references/profiles.md` teaches seeding, curation and role mapping; the generated skill copies are refreshed.
- [x] 3.4 Require a reviewed preview for every governed write that creates or changes a profile page. Verify an unreviewed edit, a reviewed edit and a stale review, and that other page types are unaffected.
  - `tests/test_profile_write_guard.py` covers the refusal, the reviewed commit, a stale review, creation, replacement, the file writer and the typed tools' `review` field.

## 4. Routing tables

This section was moved from task 3.3 of `deliver-session-context-and-skills`.

- [x] 4.1 Evaluate a pick-by-problem routing table as a vault skill against an ordinary note, on real models (design D8, round R3).
  - The instructed skill carrier beat the note per case, with an interval excluding zero, and forced no row on other turns. Design D8 records the result.
- [x] 4.2 Specify the carrier that the evaluation supports, as a delta, before code.
  - `specs/vault-skills`: routing tables travel as ordinary vault skills, and the scaffold teaches the convention.
- [x] 4.3 Teach the routing-skill convention in the generic scaffold. After install, verify a row-shaped turn on the installed service against a routing skill.
  - The scaffold's "Vault skills" section teaches the convention, and the generated skill copies are refreshed.
  - 2026-11-22, installed 0.196.0: a row-shaped turn received the vault's routing skill in its `skills` catalogue, and the skill's body named the matching row and its sources.

## 5. Verify and deliver

- [x] 5.1 Run real-model cases:
  - a domain question uses the domain profile's preferences;
  - an unrelated domain gets none;
  - a curation proposal is correct on realistic notes and waits for confirmation;
  - a stated contradicting preference;
  - an explicit request to add a line;
  - a provider outage.
  - Design D7 records the result. Use, unrelated domains, outage and an asked-for curation proposal passed. Unconfirmed profile commits on a stated preference led to the preview's `next_action`, which cut them from 7 of 8 runs to 1 of 4. An upkeep item reaching the agent is checked at installed acceptance in 5.3, because the harness cannot run the delivery path.
- [x] 5.2 Measure recall latency before and after on the recall latency instrument.
  - On a production-sized vault copy (3,329 notes), 48 turns with 3 alternating repeats each: activation candidate/main 1.016 [1.003, 1.029]. The profiles stage takes a median 10.0 ms (p95 13.6 ms).
- [x] 5.3 Run the full corpus, then independent review, CI, merge, the ordinary release and installed acceptance.
  - Shipped through releases 0.187.0 to 0.199.0, each PR reviewed and green on the full CI corpus.
  - Installed acceptance on the owner's service (0.196.0): a gaming turn served the games profile and its media ancestor, and no music profile. The next turn with the continuity token returned each held profile `unchanged`. The global profile arrived under `stance`.
  - Installed curation (0.199.0 and later): the upkeep queue held six `upkeep_profile_curation` items, and the packet delivered them to the agent. Acceptance found two misses, fixed in the archiving PR: a narrower profile flagged its ancestor, and a unit on the profile's day whose page date had no time never counted.
- [ ] 5.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.
