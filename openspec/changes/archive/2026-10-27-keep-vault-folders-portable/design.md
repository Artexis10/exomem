## Decision

Path identity belongs to the vault. Two sibling names with equal `vault_name_key` (NFC, then casefold) are one name, on every host.

## Why the vault, not the host

The vault travels between hosts. A rule that depends on the running volume produces a vault whose contents differ by where the write happened, and the difference surfaces only at export or on another machine. The exporter and `unique_path` already apply the key to files. The gap is folders and the writers that never consult siblings.

## Shape

1. `vault_name_key(name, form="NFC")` in `vault.py` is the only copy of the rule. The exporter, `audit_fix`, `unique_path` and the vocabulary folding import it; `form="NFKC"` serves the looser comparisons.
2. `canonical_vault_rel` re-spells every existing component to the sibling with the same key. An exact match wins; among several variants the first in sorted order wins. `is_casing_only_rewrite` compares NFC forms, so the symlink guard keeps its meaning.
3. Writers call `canonical_vault_rel` before the destination binds: `evidence_folder` (preserve), `client_artifacts._destination`, and the existing note create and move paths.
4. The folder creator is the backstop. It refuses a missing folder whose key matches an existing sibling. It is a local structural invariant, like a parser check, and it fires only on a name that is not portable anyway. Cost of a wrong fire: one corrected call that names the existing spelling. An existing exact folder never reaches the check, so old drift never blocks.
5. `plan_project_keys` derives a folder with `_title_case_slug`, which reuses an existing spelling first; an explicit folder that collides with a different existing spelling is refused with `PATH_VARIANT_EXISTS`.
6. The `path_variants` audit reads names only. Defects are NFC-casefold groups. Candidates are NFKC groups after dropping non-alphanumeric characters (a Unicode character class, not a word list). It is optional so a vault with old drift does not gain findings on every sweep.
7. `CASE_COLLISION` gets its own export code and a group count. The reason tells the owner to run `path_variants` in their own memory; the export itself reveals no path.

## Not done

No automatic merge of split folders, and no rename on read. A merge moves content and belongs to the owner.
