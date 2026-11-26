# Tasks

## 1. Writes converge on one spelling

- [x] 1.1 `canonical_variant` chooses the NFC spelling, then a declared or registered folder, then the fullest folder, then code-point order.
- [x] 1.2 `respell_new_path` and its store variant steer every existing folder with a variant sibling, the destination's own included, to the canonical spelling, and keep a file that already exists.
- [x] 1.3 A test shows a capture, a note and a file write into an existing split landing under the canonical spelling.

## 2. Governed merge

- [x] 2.1 `maintain_memory(mode="path-variants")` previews each split: canonical spelling, every move, blocked files, the spellings it removes once empty, and the plan id.
- [x] 2.2 Its apply refuses a changed plan, then removes each listed spelling its moves left empty and logs the removal.
- [x] 2.3 The apply moves every planned file through `move_file` with link rewrites: it previews each move, commits it with that preview's digest and retries once when the digest went stale. A remote preview and apply each run as a maintenance job.
- [x] 2.4 An end-to-end test merges a split with a linked page and an Evidence binary, applies the plan, removes the emptied folder, and passes the export collision check.

## 3. Owner sees splits

- [x] 3.1 `path_variants` runs in the default audit, and its finding names the preview.
