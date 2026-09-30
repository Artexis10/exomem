## ADDED Requirements

### Requirement: Vocabulary terms compare through one conservative fold

Exomem SHALL expose one shared fold, `vocabulary_fold.fold_term`, as the comparison key for
tags, collection claims and routing terms. The fold SHALL apply NFKC normalisation, casefold,
map runs of `_`, whitespace and `-` to one `-`, strip edge hyphens, and remove at most one
plural `-s`/`-es` (`-ies` becoming `-y`) from the final hyphen segment. It SHALL NOT remove
`-ing` or `-ed`. It SHALL never inflect a word under four letters, a non-alphabetic segment,
or a member of its `EXCEPTIONS` set, which SHALL include plurals whose singular is a different
word such as `securities`, `futures`, `sales` and `operations`, and SHALL be idempotent on its
own output.

#### Scenario: Plural and separator variants share a key

- **WHEN** `failure`, `failures` and `Failures` are folded
- **THEN** all three yield `failure`
- **AND** `x_y`, `X Y` and `x-y` all yield `x-y`

#### Scenario: Different words are kept apart

- **WHEN** `training` and `trains`, `recording` and `records`, `embedded` and `embeddings`, or `securities` and `security` are folded
- **THEN** each pair yields two different keys

#### Scenario: Meaning-bearing forms are kept apart

- **WHEN** `news`, `series`, `analysis` or `status` is folded
- **THEN** the word is returned unchanged

### Requirement: Authored tags are advised, never rewritten, at write time

A write SHALL keep its authored tags at every prominence level. When an authored tag is a
fold-variant of a tag whose canonical form has strictly more page uses, a write at `maximal`
SHALL add one single-line warning naming the canonical tag, and `note`, `add`, `edit` and
`link` SHALL surface it. Tag usage SHALL come from the one count source the maintenance mode
uses; when the lexical catalogue cannot answer, the write SHALL proceed without advice. At
`maximal` the committed response SHALL NOT repeat that advice as a `vocabulary_advisory`. The
body SHALL never be changed by this step.

#### Scenario: Maximal keeps the authored tag and advises

- **WHEN** prominence is `maximal` and a write authors `failure` while `failures` is used on more pages
- **THEN** the written page's tags contain `failure`
- **AND** the response warns that `failure` is a variant of `failures`

#### Scenario: Missing catalogue fails open

- **WHEN** the lexical catalogue is absent or stale
- **THEN** authored tags are written as authored with no tag advice

### Requirement: Tag usage counts only visible pages outside owned trees

Tag usage SHALL be counted from the lexical catalogue's per-page tags after dropping every page
the caller may not see and every page in a tree another subsystem owns (Sources, Evidence,
Records, Planning, workflow contracts, `_Schema`, `_Governance`, `_Adoption`, trash, archive,
attachments and dot directories, compared casefolded). Within a fold group, uses SHALL be
counted per writer normal form, the canonical SHALL be the most-used normal form, and a group
whose two most-used forms tie SHALL have no canonical. Group listings, counts, write-time
warnings and advisories SHALL read the same for a withheld page as for an absent one.

#### Scenario: A withheld page counts as absent

- **WHEN** a restricted caller previews tag variants on a vault where pages it may not see carry variant tags
- **THEN** the preview, advisories and warnings are identical to those on the same vault without those pages

#### Scenario: The canonical is a written form

- **WHEN** `Machine_Learning` is used on three pages and `machine-learning` on one
- **THEN** the canonical is `machine-learning`

### Requirement: Tag variant maintenance is plan-gated, batched and body-preserving

`maintain_memory(mode="tag-variants")` without `apply` SHALL be read-only and SHALL list
variant groups with each spelling's page count, the canonical form, whether the group is tied,
the number of pages pending, one bounded batch, and a `plan_id`. With `apply=true`, the exact
`plan_id` and a one-line `why`, it SHALL plan without holding the vault mutation boundary,
on the served path as in the library, and, under it, re-verify each batch page's content hash
and visibility and each rewritten group's decision, refusing as stale on any change. An
optional `exclude_groups` list SHALL keep the named fold groups out of preview and apply and
SHALL be part of `plan_id`. It SHALL rewrite a variant only when its canonical has
strictly more uses, SHALL change only the frontmatter `tags` key, SHALL leave the body and
every other key unchanged, SHALL NOT drop a comment, and SHALL NOT rewrite a page in an owned
tree. It SHALL record one log entry holding each page's before and after tags and the inverse
mapping, and SHALL refuse with `TAG_VARIANT_AUDIT_UNAVAILABLE` and write nothing when that
entry cannot be written. Repeating preview and apply SHALL converge to zero pending pages.

#### Scenario: Preview writes nothing

- **WHEN** the mode is previewed on a vault with variant tags
- **THEN** groups are listed with counts and no file changes

#### Scenario: Confirmed apply rewrites tags only

- **WHEN** a preview's `plan_id` is applied with a reason
- **THEN** the batch's pages carry the canonical tags
- **AND** their bodies are byte-identical to before
- **AND** the log entry records each page's before and after tags
- **AND** a later preview reports no pending pages

#### Scenario: An excluded group is left alone

- **WHEN** a preview and apply name one group in `exclude_groups`
- **THEN** the other groups' variants are rewritten and the excluded group's are not
- **AND** the same preview without the exclusion has a different `plan_id`

#### Scenario: A tie is never rewritten

- **WHEN** two spellings of a group are used on the same number of pages
- **THEN** the group is listed as tied and no page is planned

#### Scenario: A stale plan is refused

- **WHEN** a planned page changes, or becomes withheld from the caller, between preview and apply
- **THEN** apply fails with `STALE_TAG_VARIANT_PLAN` and writes nothing
