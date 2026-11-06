## 1. Keep an unsettled build

- [x] 1.1 Keep a cold build whose post-build census never settles, with its last
  census and no event caption, and let corpus warm-up accept it.
- [x] 1.2 Add a regression test: a warm-up whose every census confirmation sees a
  new write succeeds, and the next reader gets the newest content of every page
  without a cold build. The test MUST fail against the code before 1.1, and when
  the kept entry is captioned or paired with a context older than its census.

## 2. Confirm on a live upgrade

- [x] 2.1 Run an online-upgrade rehearsal with steady writes. Confirm that the
  standby carries `semantic_corpus` to promotion and that the promoted worker logs
  no cold corpus build.
  Verified 2026-11-03 on release 0.158.1: an online upgrade of a fresh copy of a
  12.7k-page production vault, with steady reads and writes (77 each, none failed).
  The standby built the corpus context before promotion, promotion carried
  `semantic_corpus`, and the promoted worker reused that context ("context reused")
  and logged no corpus build. The standby's build settled, so the kept-unsettled
  branch itself did not run in this rehearsal; its tests cover it.
