## 1. Plan steps before the stop (D2)

- [ ] 1.1 Run trash metadata and the curation witness ahead of the preparation stop, from one function that both storage-mode branches call. Delete the copy in `vault_store/batch.py`. Keep the heat registration in the commit.

## 2. Validation through native preparation (D1, D3)

- [ ] 2.1 Move the note tools' validation to native preparation, binding the caller's page bytes.
- [ ] 2.2 Move each other write leaf that uses `validate_only`, and the `save_source`, `save_evidence`, Records and Planning tools, to native preparation.
- [ ] 2.3 Plan the log and navigation index writes again at commit and check them again.

## 3. Names once (D4)

- [ ] 3.1 Add one names function (vocabulary resolution, then folder respelling) that every surface calls before it plans. Replace the per-leaf calls with it.
- [ ] 3.3 Make the batch write refuse a planned new folder that the names function would still steer, given the path a planned move vacates, naming the registered folder.
- [ ] 3.2 Give `reconcile-folder-names-at-write` its place in the folder step, and sensitivity governance its place in the preparation access check.

## 4. Remaining surfaces (D5)

- [ ] 4.1 Give `save_episode`, device intake and the import adoption run native preparation.
- [ ] 4.2 Keep an import run item that needs a vocabulary decision in staging with its refusal, and continue the batch.

## 5. Proof (D6, D7)

- [ ] 5.1 Add the parity test: record each write command's plan, deny each planned path in turn, and assert the same refusal at validation and commit.
- [ ] 5.2 Measure validation and commit time before and after on a generated vault of about 14,000 pages under a concurrent write stream; hold the batch on a miss (D7).
