## 1. Judge what the backup read

- [x] 1.1 Add `hash --ctime` (ctime from `fstat` after reading a regular file) and `judge RESTORED REFERENCE CUTOFF_NS OUT_DIR --root ROOT --exclude PATTERN…` with exit codes 0, 3, 4 and 1 to `cloud_vault_digest.py` (design D4). Verify: fixture-tree tests show a pass with later changes reported, a fail on a missing or altered unchanged entry, an excluded entry reported, inconclusive when nothing is old enough, and 1 on an unreadable list. It catches a drill that fails on a serving cell or passes on a backup that lost files.
- [x] 1.2 Add `cloud_restore_manifests.py exclude-patterns`, printing `CATALOGUE_BACKUP_EXCLUDES`. Verify: a test shows its output equals cellctl's list. It catches a second copy of the list.
- [x] 1.3 In `cloud_restore.sh`, capture the reference with `hash --ctime`, read the cutoff from the scratch restore Job's "restoring snapshot … at …" line right after the Job finishes (design D2), run `judge` for both roots, print the counts and map each exit code to the verdict; any unknown code fails. Verify: tests cover the cutoff parse, a missing or unparsable line failing the drill, and the mapping of each exit code. It catches an `|| true` that turns a comparison that never ran into a pass, and a cutoff later than the snapshot.
- [x] 1.4 Update `docs/runbooks/cloud-operator-restore.md`: what a drill judges, the count lines, and the `inconclusive` verdict. Verify: the runbook's drill steps match the script's log lines.

## 2. Production evidence

- [ ] 2.1 After release, run the drill on the owner's cell against `live` under an operator prod grant. Verify: the log shows judged and reported counts per root and `verdict=pass scratch_cleanup=yes rc=0`.
