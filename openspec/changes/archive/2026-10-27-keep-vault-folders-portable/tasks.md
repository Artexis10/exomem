## 1. Vault invariant

- [x] 1.1 Add `vault_name_key` and `name_variants` and use them in the exporter, `audit_fix`, `unique_path`, project keys and vocabulary folding; verify one copy of the rule remains.
- [x] 1.2 Add `respell_new_path` and call it from every writer that creates a path; verify a capture scope, an explicit create path and a move destination land in the existing spelling.
- [x] 1.3 Keep `canonical_vault_rel` host-faithful; verify a delete on one side of a split keeps the other page's index row.
- [x] 1.4 Add the `PATH_VARIANT_EXISTS` backstop in the held filesystem and the split warning; verify a refused variant and an unblocked split.

## 2. Audit, export and scaffold

- [x] 2.1 Add the `path_variants` audit category; verify it finds case and NFC groups.
- [x] 2.2 Give `CASE_COLLISION` its own export code and add it to the cellctl job failure codes; verify no path appears and the job records the code.
- [x] 2.3 Fix the scaffold contradiction and the reuse guidance; regenerate the derived skills and plugins; verify the drift tests.

## 3. Evidence back-references

- [x] 3.1 Rewrite an Evidence page's `ingested_into` entry on a move when that field holds every rewritten link; verify the other bytes stay and a body link still refuses `APPEND_ONLY`.
