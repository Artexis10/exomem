## 1. Hosted refusal guidance

- [x] 1.1 Add `_HOSTED_REFUSAL_GUIDANCE` in `src/exomem/server_hosted.py`: a static code → (message, remediation) table, with the `missing_semantic_unit` and `empty_rich_unit` entries derived from `semantic_authoring.AUTHORING_CONTRACT.findings` at import time so they cannot drift
- [x] 1.2 Author hosted entries for `SEMANTIC_CONTRACT_BLOCKED`, `RELATION_DISPOSITION_MISSING` and `RELATION_DISPOSITION_STALE`, phrased for a person rather than for an agent retry loop
- [x] 1.3 Add `_remediation_for(code)` mirroring `_message_for(code)`; have `_message_for` consult the table before its existing prefix rules
- [x] 1.4 Replace the hardcoded `"remediation": None` in `_error_response` with `_remediation_for(code)`
- [x] 1.5 Confirm no exception-derived text is introduced: the table is the only new source of message/remediation text, and `_error_response` still receives only a code plus allowlisted `details`

## 2. Tests

- [x] 2.1 `_error_response("missing_semantic_unit", …)` returns a non-null remediation and a message other than `hosted command failed`
- [x] 2.2 `_error_response("RELATION_DISPOSITION_MISSING", …)` returns a non-null remediation
- [x] 2.3 A code with no table entry still returns the generic message and `remediation: null`
- [x] 2.4 The `missing_semantic_unit` entry equals the text the authoring contract defines, so the derivation is real and not a copy that can drift
- [x] 2.5 Three consecutive ordinary sentences through `capture_source` all succeed on a fresh vault, and each is retrievable by keyword recall — the behaviour the hosted UI will depend on
- [x] 2.6 A second governed `remember` with no qualifying relation and no reviewed disposition is still refused, proving the contract was not weakened
- [ ] 2.7 Run the full pytest suite

## 3. Verification

- [x] 3.1 Re-run the boundary reproduction and confirm a refused hosted write now names its cause and carries remediation
- [x] 3.2 Confirm no change to `remember`'s accept/refuse behaviour — only to what a refusal says
- [ ] 3.3 Open a PR describing this as an error-contract change and linking the design's Risks section

## 4. Companion work (tracked in `substrate`, not this change)

- [x] 4.1 Route the hosted capture box to `capture_source` instead of `remember` — this is the fix that makes a new user able to save at all
- [x] 4.2 Make the authenticated first run connect-first: server URL, copy button, per-client paths; capture and search demoted below it
- [x] 4.3 Replace the `home-client.tsx` placeholder "Kim prefers the morning train" — one of the four alpha invitees is named Kim
- [ ] 4.4 End-to-end acceptance on a fresh reviewer tenant: connect-first landing, one ordinary sentence saves, and `Find something` returns it
