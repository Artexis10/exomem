## 1. Specification

- [x] 1.1 Write the proposal, the design and the four deltas, and correct the active baseline delta that named the capture reminder as a carrier. Validate strictly with the CI-pinned OpenSpec.

## 2. Doctrine constants

- [x] 2.1 Add the `proactive_capture` meaning map and extract the shared doctrine constants, with the bootstrap bytes unchanged.

## 3. Server carrier

- [x] 3.1 Build the carrier, attach it in `op_activate_context` outside the packet cache, and reserve its characters inside `limit_chars` while the map keeps its smallest budget.
- [x] 3.2 Carry the delivered digest in the continuity token so delivery needs no server state and holds over the stateless transport.

## 4. Hook

- [x] 4.1 Render a served block first in the retrieve hook; regenerate the plugin copies and the Cloud hook bundle.

## 5. Additive Entity fact

- [x] 5.1 Expose `create_missing` on `edit_section`, refuse a heading that is empty or spans lines, name the exact retry in both refusals with the tool names the serving surface lists, and update the scaffold route.
- [x] 5.2 Regenerate the tool schemas, tool-surface contract, ChatGPT pending digest, capabilities documentation and skill stamps; raise the tool-schema budget by the measured bytes.

## 6. Guarding tests

- [x] 6.1 Add the tests for authority agreement, staleness, re-arm, stateless delivery, budget, hook render, ceiling, and the edit and observe remediations. Watch each fail on the unfixed code or on a mutation of the fix.

## 7. Round A on real models

- [x] 7.1 Run five sessions on the candidate: three positive and two negative cases. Run two positive cases hookless as residual evidence. Result on candidate 4611400c1: hooked B1, B3 and B7 wrote; B2 and B9 wrote nothing; the block rendered first in all five; hookless B3 and B7 made no Exomem call and no write (residual evidence).

## 8. Acceptance handoff

- [ ] 8.1 Hand the 13-case acceptance to `capture-durable-personal-baselines` 4.5 and `preserve-adopted-generated-artifacts` 4.4.

## 9. Archive

- [ ] 9.1 After merge and round A, synchronize the deltas into the canonical specs and archive this change with `openspec archive`.

## 10. Round-3 repair

- [x] 10.1 Add `envelope.LINK_ACCEPTANCE_MEANING`; reword `_BASELINE_CAPTURE` and `_ADOPTION_TRIGGER`; add `_DELIVERY_CAPTURE`; make `prominence.capture_policy` take the vault's `link_acceptance`.
- [x] 10.2 Carry `link_acceptance` in `capture_carrier.block()`; raise the hook cap to 1,600 characters with a lead-sentence fallback; regenerate the plugin hook copies.
- [x] 10.3 Add the three `CORE_RULES` entries and the carrier tests; watch each fail on 49e03a6b2. Recover the 250 compact-core bytes from the compact profile and measure every level and surface cell.
- [x] 10.4 Pass `vault_root` to `stage_artifact` in both adoption paths, so a held upload (`exomem-held:`) is redeemed, and put a refused adoption's hold back for a retry. Before this, `preserve_artifacts` with an `adoption` envelope on a held handle failed with `HELD_UPLOAD_UNAVAILABLE` (round-3 case A1). This fix rides in the same batch.
- [ ] 10.5 Run the repair round on the merged candidate: B8, A1, A4, B7 and A2. When all pass, run the full 13-case acceptance.
