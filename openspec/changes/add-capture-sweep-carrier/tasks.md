## 1. Red-first tests

- [x] 1.1 Add `tests/test_capture_sweep.py`: first write from a key emits `quiet-interval`; a second write inside the interval emits nothing but still records; a write after the interval emits again; `light`/`off` never emit; stdio and outside-call use the process key; an HTTP call on the `session:`/`None` tier emits nothing; `written_recently` caps and orders newest-first; unpaged-mention extraction; fail-open when identity or the ledger raises; eviction at the ledger cap.
- [x] 1.2 Add the `tests/test_mutation_terminal.py` cases: the leaf is lifted, validated, capped, absent rather than null, stripped from the `legacy` detail, and emitted at most once per batch.
- [x] 1.3 Add `tests/test_capture_sweep_journey.py`: seed a client entity page and a prior dated note, recall, write a meeting page whose advisory names the unpaged booking system, write the quirk page with no advisory, and replay both writes proving no duplicate page and no duplicate fact.
- [x] 1.4 Observe every one of the above red before implementing, and keep the red output with the delivery.

## 2. Carrier

- [x] 2.1 Add `src/exomem/capture_sweep.py`: the bounded in-memory ledger with an injected clock, the `QUIET_SECONDS` module constant with no environment override, the caller-scoped key with its four tiers, `boundary`, `hints`, `block`, and a test reset.
- [x] 2.2 Gate emission on the resolved delegation envelope's proactive-capture disposition, read through the existing prominence and envelope resolution.

## 3. Seams

- [x] 3.1 Attach the produced block beside `structure_suggestion` and `due_state` in `semantic_writes.commit_existing` and `commit_creation`, fail-open exactly as the two existing tenants are, after the guarded write returns.
- [x] 3.2 Attach it from the structured-write carrier in `records.py` so `record_memory` and `plan_memory` writes are captures too.
- [x] 3.3 Record the ledger from Sources and Evidence writes only where they already pass the mutation terminal; add no new seam, and report which of them do and do not. Result: `preserve_artifacts`, `process_media`, `adopt_vault`, `adoption_studio` and `maintain_memory` reach the shared batch carrier and therefore record and may emit; `capture_source` and `preserve_evidence` do not reach it and write through `add`/`client_artifacts` rather than the page or structured seams, so they neither record nor emit, and giving them the ledger would need the new seam this task forbids.
- [x] 3.4 Lift, validate, cap and attach it in `mutation_terminal` exactly as `structure_suggestion` is, and strip it from the `legacy` detail.
- [x] 3.5 Compose at most one block per batch in `commands.py`, mirroring `_carrying_due_state`, and prove it with a test.

## 4. Contract text

- [x] 4.1 Append the clause to the `balanced` and `maximal` prominence capture strings, leaving `light` and `off` untouched.
- [x] 4.2 Add the `capture_sweep_handling` entry beside `due_state_handling` and to `_SESSION_POST_WRITE_KEYS`.
- [x] 4.3 Mirror the clause into the shipped scaffold engagement reference and regenerate the local plugin skill copies from it.
- [ ] 4.3a BLOCKED, and deliberately so: the two copy-paste blocks in `docs/prominence.md` measure 1,484 and 1,495 bytes against a 1,500-byte cap, so no wording of the clause fits and no existing capture class is shortened to make room. Those clients receive the doctrine through the compact bootstrap payload the blocks already direct them to. Revisit when a trim frees the room.
- [x] 4.4 Enumerate the doctrines each touched carrier held before the change and confirm every one survives; record the check with the delivery.
- [x] 4.5 Measure the compact bootstrap payload at `balanced` and `maximal` before and after and record both numbers. The ceiling is not raised.

## 5. Verification

- [x] 5.1 Run the scoped suites: capture-sweep, mutation terminal, prominence, bootstrap compact budget, scaffold leak, workflow contracts, records, and the cross-cutting hosted-plugin, tool-surface, plugin-sync, schema-fidelity, ChatGPT and egress-receipt contract suites. Record what was run.
- [x] 5.2 Run `ruff check` on every changed path, `openspec validate --all --strict` with the CI-pinned validator, and the archive-discipline check.
- [x] 5.3 Confirm the packaged tool-surface digest has not moved.
- [ ] 5.4 File the follow-up change `amend-no-nudge-bench-families-seq4` for the f28 real-agent replay family, and claim no comparative no-nudge result until it exists.
- [ ] 5.5 Independent author-blind review of the carrier, the ledger key, the once-per-batch composition, and the carrier-text superset.
- [ ] 5.6 Full-corpus run at the delivery boundary.
- [ ] 5.7 Delivered: PR merged and change archived.

## Follow-ups (outside this change)

Named so they are not lost, and deliberately NOT checkboxes: this change must not
be archive-gated on work another owner schedules.

- Hosted agent skill (design D7), owned by the hosted release owner: carry the
  clause at the next hosted candidate mint. `plugins/hosted/skills/exomem/SKILL.md`
  feeds the release-locked v1 hosted identity and is deliberately untouched here;
  hosted clients receive the doctrine through the bootstrap payload meanwhile.
