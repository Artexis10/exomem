## Why

Until 0.186.0 a new page took a slug name. Since then a vault's `filename_style` decides, and the default is the title. A retitle never renamed a page either. On a long-lived vault, thousands of pages keep a file name that no longer follows their title, and nothing tells the connected agent. The operator ruled that titles are the standard and wants the backlog converted in large approved batches.

## What Changes

- Add one detector. A page drifts when its file name differs from the name that the creation path gives its frontmatter title today, under the vault's filename style. A collision number on a matching name is not drift. The scope comes from the note-type registry attribute that names a type's files by title, less captured Sources, which keep dated names.
- Add the review family `name_drift`: one item for the whole vault while any page drifts. Its fingerprint changes with the drifted set. It carries the spelled-out `curation.relocate` call that renames the next batch.
- Add a bounded, paginated read operation, `inspect_name_drift`, that lists each drifted page with its ref, title, current name and planned name. It reports the total, and says so when the scan did not finish. A page with no usable title is marked and never proposed.
- Return a `structure_suggestion` of kind `rename`, with the spelled-out call for one page, from a write that changes a page's title so that its name drifts.
- Teach the scaffold that the user's own retitle is the approval, and that a backlog batch goes to the user as one preview.

## Capabilities

### New Capabilities

### Modified Capabilities

- `attention-queue`: adds the `name_drift` family and the `rename` write suggestion.
- `vault-file-naming`: the rule that no existing file is renamed stays true for the write path and a style change. It gains the detection of names that follow no title, and the exception that a curation relocation may rename a drifted page.

## Impact

- Code: a new module `name_drift` that holds the rule, the batch resolver, the review finding, the listing and the write suggestion; one audit check; one attention category; one read command; one terminal projector.
- Depends on `curation.relocate` (separate change) to apply a rename. This change builds no rename.
- Agent surface: one read operation, one review family and one more `structure_suggestion` kind.
