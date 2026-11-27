## 1. One plan function (D1, D2)

- [ ] 1.1 Move the lock-free part of the batch write into one plan function: complete the plan (leaf, log, navigation index, auxiliary, trash metadata and curation witness writes) and run the access check on every planned write. Both storage-mode branches and curation's preparation stop call it; delete the copy in `vault_store/batch.py`. Keep the heat registration in the commit.
- [ ] 1.2 In store mode, make validation read the store's availability and return the refusal a commit would get for a diverged store or a pending takeover.

## 2. Validation through the plan function (D1, D3)

- [ ] 2.1 Make the note tools validate through the plan function, read-only, binding the caller's page bytes.
- [ ] 2.2 Do the same for each other write leaf that uses `validate_only`, and for the `save_source`, `save_evidence`, Records and Planning tools.
- [ ] 2.3 Plan the log, navigation index, trash metadata and curation witness writes again at commit and check them again.

## 3. Names once (D4)

- [ ] 3.1 Add one names function (vocabulary resolution, then folder respelling) that every surface calls before it plans. Replace the per-leaf calls with it.
- [ ] 3.2 Give the planned `reconcile-folder-names-at-write` its place in the folder step, and sensitivity governance its place in the plan function's access check.
- [ ] 3.3 Make the plan function refuse a planned new folder that the names function would still steer, given the path a planned move vacates, naming the registered folder.

## 4. Remaining surfaces (D5)

- [ ] 4.1 Validate `save_episode` and device intake through the plan function, and make the import run call the names function.

## 5. Proof (D6, D7)

- [ ] 5.1 Add the parity test: record each write command's plan, deny each planned path in turn, and assert the same refusal at validation and commit.
- [ ] 5.2 Measure validation time, commit time and the store boundary hold before and after, on a generated vault of about 14,000 pages under a concurrent write stream; hold the batch on a miss (D7).
