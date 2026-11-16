# Tasks

## 1. Settle placement with the layout owner

- [x] 1.1 Confirm the profile area with the single-store layout decision; record the shipped `profile` type, its folder and domain partition.
  - The layout owner confirmed that the root-native layout is group 9 of `make-vault-store-canonical`. Until a relayout runs, new pages use the current layout, and compiled areas follow the note-type registry folder at relayout.
  - The owner chose one profile area with no domain partition: folder `Notes/Profiles`, with the domain in `topics` (design D1).

## 2. Domain profiles

- [ ] 2.1 Add the `profile` note type and the domain `parent` field. Verify placement of a global and a domain profile, rejection of a two-topic profile, and rejection of an unknown parent and a cycle.
- [x] 2.2 Run round R1 on a realistic fixture with keys written from the turn text, and record the selection arm in design D6.
  - Arm (c) won on the test half, with intervals that exclude zero against both other arms. Design D6 records the result and the shipped rule.
- [ ] 2.3 Serve `domain_profiles` with the selected arm, inheritance, both budgets, the off switch, egress and the terminal guard. Verify the gaming, two-domain, general-knowledge and over-budget scenarios, and terminal egress on a release-gated vault.
- [ ] 2.4 Carry profile generations in continuity, and render profiles in the hook. Verify a later same-domain turn and an edited profile, and add one hook test on a real service packet.
- [ ] 2.5 Supply stance from a profile page with no topics. Verify that a retyped global page serves the same lines.

## 3. Profile curation

- [ ] 3.1 Add the `upkeep_profile_curation` family. Verify detection of new units, a vault-mapped category, supersession, contradiction, expiry and `check_by`, all without a model.
- [ ] 3.2 Surface due items through upkeep delivery, and verify that a dismissal holds until the evidence changes.
- [ ] 3.3 Teach seeding and curation in the generic scaffold: the agent proposes exact lines, the owner confirms, and a vault maps its own preference categories to roles.
- [ ] 3.4 Require a reviewed preview for every governed write that creates or changes a profile page. Verify an unreviewed edit, a reviewed edit and a stale review, and that other page types are unaffected.

## 4. Routing tables

This section was moved from task 3.3 of `deliver-session-context-and-skills`.

- [x] 4.1 Evaluate a pick-by-problem routing table as a vault skill against an ordinary note, on real models (design D8, round R3).
  - The instructed skill carrier beat the note per case, with an interval excluding zero, and forced no row on other turns. Design D8 records the result.
- [x] 4.2 Specify the carrier that the evaluation supports, as a delta, before code.
  - `specs/vault-skills`: routing tables travel as ordinary vault skills, and the scaffold teaches the convention.
- [ ] 4.3 Teach the routing-skill convention in the generic scaffold. After install, verify a row-shaped turn on the installed service against a routing skill.

## 5. Verify and deliver

- [ ] 5.1 Run real-model cases:
  - a domain question uses the domain profile's preferences;
  - an unrelated domain gets none;
  - a curation proposal is correct on realistic notes and waits for confirmation;
  - a stated contradicting preference;
  - an explicit request to add a line;
  - a provider outage.
- [ ] 5.2 Measure recall latency before and after on the recall latency instrument.
- [ ] 5.3 Run the full corpus, then independent review, CI, merge, the ordinary release and installed acceptance.
- [ ] 5.4 Synchronize and archive after the nonoptional tasks have code, test and merge evidence. Run `openspec validate --all --strict` before and after.
