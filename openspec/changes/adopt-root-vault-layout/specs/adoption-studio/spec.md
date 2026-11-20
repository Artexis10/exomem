## MODIFIED Requirements

### Requirement: Exact-action preview bound by plan identity

`adoption_studio(action="plan")` SHALL read and sha256-hash each selected file at plan time, compute the exact deterministic target path, title, and frontmatter for each item, record any skipped items with a code, and persist a `plan` carrying a `plan_id` derived from the selection hash and the complete planned actions: each item's destination, classification, provenance fields, rendered link projection, and original hash. Destinations SHALL be allocated before links resolve and SHALL NOT change afterwards. A text item that is not valid UTF-8 SHALL plan as an artifact. A classified item's target SHALL be in its kind's Source directory (`Sources/<Kind>/<Domain>/` in a `nested` vault and `Sources/<Kind>/` in a `root` vault, relative to the managed root); an unclassified item's target SHALL be in `Sources/Imported/`. For a text item, `plan` SHALL read its wikilinks, embeds, and local Markdown links and images with the shared parsers, and SHALL resolve each target against the planned items and earlier imports by exact collection-relative path, then note-relative path, then unique basename, then unique front-matter alias, leaving an ambiguous or missing target unresolved, and SHALL record the resolved targets, the unresolved targets, the aliases it carries, its tags, and any tag the vault's tag normalization rejects. A text item's tags SHALL be its front-matter tags followed by the inline `#tags` of its body that the shared Markdown parser reads outside code, comments and escapes; a hash inside code SHALL NOT become a tag. `plan` SHALL check each item against earlier imports by `import_collection` and `collection_path` (falling back to `imported_from` for an import that lacks them, and reading each `also_imported_from` entry of a Source as an earlier import of its own), SHALL record `existing_ref` for an item whose original an earlier import holds with the same bytes, and SHALL record the latest earlier Source as `previous_import` for an item whose bytes changed. `plan` SHALL also check each item's bytes against the vault's stored originals and Source payloads of the same lane through the duplicate-handling index, and against every earlier item of the same plan. An item whose bytes the vault stores under another path or collection SHALL record `existing_ref` and the import mapping that `apply` would add. An item whose bytes an earlier item of the plan holds SHALL record `duplicate_of` naming that item, SHALL plan no Source or artifact of its own, and SHALL resolve links to it as links to that earlier item. The plan SHALL report the total bytes that `apply` will write, which exclude every such item. A warning the plan raises, such as an earlier import of the collection from another root, SHALL appear in the plan's `warnings` and in the top-level `warnings` of every reply for that run, where the mutation terminal reads the warning texts and the warning count. When the remaining request budget falls below its margin, `plan` SHALL stop, return `progress` and a `continuation`, and continue on a call that passes that cursor. `plan` SHALL write nothing outside the run object (targets are previewed, not created). The preview a caller sees SHALL be exactly what a subsequent `apply` commits, or `apply` SHALL refuse.

#### Scenario: Plan previews exact targets without writing

- **WHEN** `plan` runs over a non-empty selection
- **THEN** each item records its `original_sha256`, exact `target_path`, `title`, and `frontmatter`, and a stable `plan_id` for identical inputs
- **AND** a byte snapshot of the vault before and after `plan` is identical outside the run object

#### Scenario: A root vault plans a classified item without a domain folder

- **WHEN** `plan` runs in a `root` vault over an item classified with a kind and a domain
- **THEN** the item's `target_path` is under `Sources/<Kind>/` with no domain folder
- **AND** the item still records its domain in its classification

#### Scenario: Replacement cannot redirect an original read

- **WHEN** a selected original or its parent is replaced with a link during planning or apply
- **THEN** retained no-follow custody prevents the read from following that link
- **AND** a changed binding produces a coded refusal before publication instead of importing bytes from the replacement

#### Scenario: Plan resolves links without guessing

- **WHEN** a planned note links to another planned note by path, embeds a planned image by basename, links to a third note through its front-matter alias, and links to a basename that two planned notes share
- **THEN** the first three targets are recorded as resolved to those items' frozen target paths
- **AND** the shared basename is recorded as unresolved

#### Scenario: Plan reads inline tags outside code

- **WHEN** a planned note carries a front-matter tag, an inline `#tag` in its body, and a hash inside an inline code span and inside a fenced code block
- **THEN** the item's tags are the front-matter tag followed by the inline tag
- **AND** neither hash inside code is a tag

#### Scenario: Plan recognizes an earlier import

- **WHEN** a collection that an earlier run imported is staged again under a new location with the same `collection` label and a single note changed
- **THEN** every unchanged item records the `existing_ref` of its earlier Source
- **AND** the changed note plans as a new item that names its earlier Source as `previous_import`

#### Scenario: Apply refuses a stale plan

- **WHEN** `apply` is called with a `plan_id` that does not equal the stored plan's id, or the current selection hash no longer matches the plan's selection hash
- **THEN** it is refused with `PLAN_STALE` and writes nothing

#### Scenario: A tree holds two identical photos

- **WHEN** `plan` runs over a tree whose two photos have identical bytes in different folders
- **THEN** the first photo plans as a new Source with its artifact
- **AND** the second records `duplicate_of` naming the first and plans no Source or artifact
- **AND** the reported bytes to write count the photo once

#### Scenario: A tree repeats bytes the vault already stores

- **WHEN** `plan` runs over a file whose bytes a Source from another import already stores under a different collection path
- **THEN** the item records that Source's `existing_ref` and the mapping `apply` would add
- **AND** the plan writes nothing outside the run object
