## 1. Folder key and write-time steering

- [x] 1.1 Add `vault.folder_lookalike_key` (NFC casefold; Unicode Zs, Pd and Pc removed; a leading run of Pc kept).
- [x] 1.2 Add `leaf_is_folder` to `respell_new_path`; in it and `_respell_in_store`, steer a folder the walk would create into its one look-alike sibling folder, or the group's `canonical_variant`; skip the vault top, the managed root, declared folders, a registered project folder in the research folder, file leaves and the vacated path.
- [x] 1.3 Pass `leaf_is_folder=True` from Evidence capture, note placement, client artifact folders and create_directory.
- [x] 1.4 Compute Evidence family tags, the activity line and the log entry from the landed segments.
- [x] 1.5 Match project-key folder planning by the folder key.
- [x] 1.6 Steer a write into an existing separator pair's canonical spelling, and name the pair in `folder_split_warnings`.
- [x] 1.7 Keep a separator between two digits in the key, and leave the Sources tree out of the separator rule (`vault.separator_rule_applies`).

## 2. Merge and audit

- [x] 2.1 Group `path_variants` splits and the emptied-folder check by the folder key.
- [x] 2.2 Audit: defect for a folder-key group, candidate only for a group the alphanumeric key alone joins.

## 3. Proof and delivery

- [x] 3.1 A test per spec scenario, in file mode and store mode where the path differs; each fails without its code change.
- [x] 3.2 Scoped suites for vault respelling, preserve, notes, create, move, path_variants, audit, project keys and store writes; strict OpenSpec gate.
- [x] 3.3 One blind review; merge; archive the change with `openspec archive`.
