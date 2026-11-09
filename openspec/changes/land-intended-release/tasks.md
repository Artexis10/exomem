## 1. Land an intended release

- [x] 1.1 Accept a reviewed head on release intent and keep the intent as a short-lived artifact
- [x] 1.2 Add `land`: match the intent to the open release PR's exact source and head, wait for required checks, then run the guarded merge with the release App token
- [x] 1.3 Run `land` after the evidence refresh, and in the intent job when evidence was reused; honour `RELEASE_AUTO_MERGE=off`
- [x] 1.4 Test that only the named head merges, and that a nightly Full run, a newer head or a failed check never merges
- [x] 1.5 Update the release runbook

## 2. Verify on a real release

- [ ] 2.1 The next release dispatched with its reviewed head merges itself after its required gate passes, and Release Please tags it
