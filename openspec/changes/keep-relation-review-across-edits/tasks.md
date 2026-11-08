## 1. Carry a reviewed-none decision across edits

- [x] 1.1 Carry the before state's reviewed-none decision to the after state of a
  non-create-like precommit operation, in `semantic_contract.evaluate`, and report
  `review_renewed`. Evidence: `tests/test_semantic_contract.py` (survives an edit with a
  peer, exact review not renewed, creation never carries, contested identity, bootstrap
  not carried).
- [x] 1.2 Keep refusing an edit that removes the last qualifying connection. Evidence:
  `tests/test_semantic_contract.py` last-connection-removal case, grandfathered and not.
- [x] 1.3 Read a carried decision through the committed slot's `carried_from`, and record
  it on edit commit. Evidence: `tests/test_semantic_lifecycle_writers.py` repeated edits
  write no decision file, and a handshake decision stays in force on later edits.

## 2. One-call routes

- [x] 2.1 Commit reviewed-none in one call for a creation that names no draft field.
  Evidence: `tests/test_semantic_creation_writers.py` one-call creation and the
  validated-draft case that still names the missing hash.
- [x] 2.2 Commit reviewed-none in one call for an edit that names no `transition_token`,
  and record a decision only when the edit needs one. Evidence:
  `tests/test_semantic_lifecycle_writers.py` one-call edit cases.

## 3. Agent-facing text

- [x] 3.1 Name a route that the called tool offers in each relation remediation, spelled
  for the active surface. Evidence: `tests/test_disposition_remediation_is_route_neutral.py`.
- [x] 3.2 Shorten the bootstrap rule texts and the remediation text, and leave parameter
  descriptions alone. Evidence: `tests/test_bootstrap_core.py` populated-core ceiling
  holds with no growth in any cell.
- [x] 3.3 Update the scaffold `writing.md` and `mutation-results.md` and regenerate the
  plugin copies. Evidence: `cloud-plugin.py check` and `refresh-skill-contract.py --check`.

## 4. Proof

- [x] 4.1 Run the scripted no-model proof on main and on the branch, and record calls per
  saved write, refusals by code, decision files per page and audit findings.
- [x] 4.2 Run `openspec validate --all --strict` and the generator checks.
- [ ] 4.3 Replay the first attempt of each write from the benchmark's Exomem arm, with
  review fields removed, against main and the branch, and report refusals by code.
- [ ] 4.4 Rerun the benchmark's dev round on a build of this change, and report refusals by
  code, calls per saved write, and the per-case score difference with its interval.
