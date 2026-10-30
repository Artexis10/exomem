## 1. Slice compile

- [ ] 1.1 Pass the request's `conversation` and `trigger` from the `maps` branch into the slice's `_compile_guarded_activation` call, with no continuity, `anchor`, `read_thread`, attribution or `intent_resolution`.
- [ ] 1.2 Carry the slice compile's `generation.conversation` state back through the slice, and report it on the `maps` packet; report `absent` when no slice compile reads the conversation.
- [ ] 1.3 Report `generation.continuity` in `maps` mode from the token: `absent`, `applied` for a token with a map generation, `stale` otherwise.
- [ ] 1.4 Change the slice's fixed description to name the conversation.
- [ ] 1.5 Make the retrieve hook label a slice lead with `conversation` evidence, if it does not today.
- [ ] 1.6 Update the scaffold and documentation text that says `maps` mode ignores `conversation`.

## 2. Proof

- [ ] 2.1 Test that a follow-up whose words reach an invented anchor only partially lists it as `resolved` with `conversation` evidence when the earlier entry names it, and as `partial` without the conversation.
- [ ] 2.2 Test that an anaphoric follow-up is carried from the newest earlier user entry as `partial` `[conversation]`, and is not without the conversation.
- [ ] 2.3 Test with twin vaults that a restricted caller's conversation naming a withheld anchor and page gives byte-identical packets.
- [ ] 2.4 Test the `generation.continuity` and `generation.conversation` reports, including a token after a write that moved the map generation, and the setting off.
- [ ] 2.5 Run the existing leads-only versus full-compile equality test with a conversation.
- [ ] 2.6 Add a conversation case to the latency gate's paired slice-on-minus-off difference, under the existing bound.
- [ ] 2.7 Prove that a `maps` request with a conversation and a token writes no session, heat or learning record.

## 3. Verification and delivery

- [ ] 3.1 Run the scoped activation, slice, maps, conversation and latency tests and compare failure names with the `origin/main` baseline; run Ruff, the privacy gate and `openspec validate --all --strict`.
- [ ] 3.2 After release, replay a recorded turn and its follow-up on the installed service with the conversation that the hook sends, and record the slice, the reports and the timings beside the same request on the earlier release.
