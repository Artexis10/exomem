# Tasks

## 1. Writes converge on one spelling

- [x] 1.1 `canonical_variant` chooses the registered folder, then the fullest folder, then code-point order.
- [x] 1.2 `respell_new_path` and its store variant steer an existing directory component with a variant sibling to the canonical spelling, and keep a path that already exists.
- [x] 1.3 A test shows a capture, a note and a file write into an existing split landing under the canonical spelling.

## 2. Governed merge

- [x] 2.1 `maintain_memory(mode="path-variants")` previews each split: canonical spelling, files to move, empty variant folders, and the plan id.
- [x] 2.2 Its apply removes only the empty variant folders that the preview listed and logs them; a folder that gained an entry is refused as stale.
- [x] 2.3 The apply moves a bounded batch through `move_file` with link rewrites: it previews each move, commits it with that preview's digest, retries once when the digest went stale, and stops when the request budget runs short.
- [x] 2.4 An end-to-end test merges a split with a linked page and an Evidence binary, applies the batch, removes the emptied folders, and passes the export collision check.

## 3. Owner sees splits

- [x] 3.1 `path_variants` runs in the default audit, and its finding names the preview.
