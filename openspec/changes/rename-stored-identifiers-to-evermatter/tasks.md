# Tasks

## 1. Note identity: readers and writers (D1, D2)

- [ ] 1.1 Add one accessor that reads `evermatter_id` then `exomem_id`, and one writer that writes `evermatter_id`. Route every read and write of note identity through them.
- [ ] 1.2 Report a note that holds both keys with different values as an audit integrity error.
- [ ] 1.3 Update the scaffold's frontmatter reference and skill text to name `evermatter_id`.

## 2. Note identity: the rewrite job (D3)

- [ ] 2.1 Add the governed rewrite job: preview with a plan ID, copy before change, atomic per note, resumable, receipt, rollback from the receipt.
- [ ] 2.2 List in the preview the notes whose text names `exomem_id`.
- [ ] 2.3 Run the job on a copy of a production-sized vault and record the counts, the duration and a rollback.
- [ ] 2.4 After `move-cloud-to-evermatter-hosts` completes, run the job on Cloud vaults one cell at a time under a production grant.

## 3. Reference scheme (D4)

- [ ] 3.1 Emit `evermatter://` for new references from one emitter. Resolve both schemes.

## 4. Package and state paths (D5, D6)

- [ ] 4.1 Rename the import package to `evermatter` and ship the `exomem` alias package.
- [ ] 4.2 Move the state root through the crash-safe relocation, behind an explicit command that stops the service first.

## 5. Cloud infrastructure names (D7)

- [ ] 5.1 Write a runbook per family (Helm release, namespaces, secrets, images, cellctl) and run each under a production grant.

## 6. Compatibility window (optional for archive)

- [ ] 6.1 On or after 2027-11-23, review use of the old readers, the alias package and the old state paths, and remove each only with evidence.
