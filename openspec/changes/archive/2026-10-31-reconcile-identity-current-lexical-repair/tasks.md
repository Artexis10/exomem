## 1. Reconciled repair

- [x] 1.1 Seed the `rebuild_atomic` replacement from the live catalogue through SQLite's online backup when the live store exists, is neither retired nor bound for quarantine, passes the quick check, and carries the current schema, the captured catalogue identity and a fresh build's declarations.
- [x] 1.2 Add `_reconcile_catalog`, which takes a catalogue connection and the walk and keeps a page only when its content hash, mtime, scope membership and semantic generation are current.
- [x] 1.3 Fall back to the whole-corpus build from an empty temp for every other case and for a copy or reconcile error, and log the route with page counts and elapsed milliseconds.

## 2. Proof

- [x] 2.1 Test that a repair of an identity-current catalogue parses only the edited and added pages and publishes what a fresh build publishes.
- [x] 2.2 Test that a replacement that keeps its mtime is re-parsed and found by search.
- [x] 2.3 Test that an identity change parses every page.

## 3. Verification and delivery

- [x] 3.1 Run the lexical module and the scoped lexical, rebuild, catalogue and recall suites, and compare failure names with the `origin/main` baseline.
- [x] 3.2 Time a repair of a generated 2,000-page vault with five changed pages on both routes.
- [x] 3.3 Run Ruff, the privacy gate and `openspec validate --all --strict`.
