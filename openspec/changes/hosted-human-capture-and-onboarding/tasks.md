## 1. Error fidelity

- [ ] 1.1 Add a `ValueError` subclass carrying `.code`, `.reason`, `.missing` and `.remediation`, whose `str()` is byte-identical to today's flattened message so existing `except ValueError` handlers and message assertions keep working
- [ ] 1.2 Replace the 26 `raise ValueError(f"{e.code}: {e.reason}…")` sites in `src/exomem/commands.py` by one deterministic transform keyed on the exact shape; assert the substitution count is exactly 26 and the diff is that many times the clause length
- [ ] 1.3 Confirm the flattened sites are character-identical modulo the `(missing: …)` variant, and that the variant is handled rather than skipped
- [ ] 1.4 Make `relation_review._translate` use the preserved `.code` instead of falling through to `SEMANTIC_CREATION_FAILED`, and carry `.remediation` onto the returned error
- [ ] 1.5 Distinguish "no remediation was produced" from "remediation was lost" in the response shape

## 2. Tests

- [ ] 2.1 A write refused for a missing semantic unit reports `missing_semantic_unit` with remediation text, not a generic code — assert the exact code, since the whole point is that it is specific
- [ ] 2.2 A write refused for a missing relation disposition reports its own distinct code with remediation
- [ ] 2.3 An unclassified failure still reports the generic code and states that no remediation is available
- [ ] 2.4 Three consecutive ordinary sentences through `capture_source` all succeed on a fresh vault, and each is retrievable by keyword recall — this is the behaviour the hosted UI depends on
- [ ] 2.5 A second governed `remember` with no qualifying relation and no reviewed disposition is still refused, proving the contract was not weakened
- [ ] 2.6 Run the full pytest suite; any test asserting the old flattened-message behaviour is updated deliberately, with the reason recorded, not silenced

## 3. Verification

- [ ] 3.1 Re-run the local reproduction (`repro_fresh_vault.py`, `repro2.py`, `repro5.py`) and confirm each refusal now names its cause and carries remediation
- [ ] 3.2 Confirm no change to `remember`'s accept/refuse behaviour — only to what a refusal says
- [ ] 3.3 Open a PR describing this as an error-contract change and linking the design's Risks section
- [ ] 3.4 After deploy, confirm the hosted response for an unstructured write names the real cause instead of `SEMANTIC_CREATION_FAILED / remediation: null`

## 4. Companion work (tracked in `substrate`, not this change)

- [ ] 4.1 Route the hosted capture box to `capture_source` instead of `remember`
- [ ] 4.2 Make the authenticated first run connect-first: server URL, copy button, per-client paths; capture and search demoted below it
- [ ] 4.3 Replace the `home-client.tsx` placeholder "Kim prefers the morning train" — one of the four alpha invitees is named Kim
- [ ] 4.4 End-to-end acceptance on a fresh reviewer tenant: connect-first landing, one ordinary sentence saves, and `Find something` returns it
