## ADDED Requirements

### Requirement: Vocabulary terms compare through one conservative fold

Exomem SHALL expose one shared fold, `vocabulary_fold.fold_term`, as the comparison key for
tags, collection claims and routing terms. The fold SHALL lowercase, map `_` and whitespace
runs to `-`, and remove at most one plural `-s`/`-es` and one `-ing`/`-ed` inflection from the
final segment, the latter only when the remaining stem has four or more letters. It SHALL
never inflect a word under four letters, a non-alphabetic segment, or a member of its
`EXCEPTIONS` set, and SHALL be idempotent on its own output.

#### Scenario: Inflection and separator variants share a key

- **WHEN** `dogfood`, `dogfooding` and `Dogfooding` are folded
- **THEN** all three yield `dogfood`
- **AND** `x_y`, `X Y` and `x-y` all yield `x-y`

#### Scenario: Meaning-bearing forms are kept apart

- **WHEN** `news`, `series`, `analysis` or `status` is folded
- **THEN** the word is returned unchanged

### Requirement: Authored tag variants reconcile at write time

When an authored tag is a fold-variant of an existing tag with strictly higher page usage, a
write at prominence `maximal` SHALL record the canonical spelling in place of the authored one
and report the substitution as a one-line warning. At any other level the write SHALL keep the
authored tag. Tag usage SHALL come from the lexical catalogue; when the catalogue cannot
answer, the write SHALL proceed unchanged. The body SHALL never be changed by this step.

#### Scenario: Maximal records the canonical tag

- **WHEN** prominence is `maximal` and a write authors `dogfooding` while `dogfood` is used on more pages
- **THEN** the written page's tags contain `dogfood` and not `dogfooding`
- **AND** the response warns that `dogfooding` was recorded as `dogfood`

#### Scenario: Missing catalogue fails open

- **WHEN** the lexical catalogue is absent or stale
- **THEN** authored tags are written as authored

### Requirement: Tag variant maintenance is plan-gated, batched and body-preserving

`maintain_memory(mode="tag-variants")` without `apply` SHALL be read-only and SHALL list
variant groups with each spelling's page count, the canonical spelling, the number of pages
pending, one bounded batch, and a `plan_id`. With `apply=true`, the exact `plan_id` and a
one-line `why`, it SHALL rewrite that batch's minority variants to the canonical tag through
the governed write path under the mutation guard, with per-page path guards and expected
hashes, and SHALL record one log entry. It SHALL change only the frontmatter `tags` key,
SHALL leave the body byte-identical, SHALL NOT rewrite Sources or Evidence, and SHALL refuse a
plan whose mapping or pages changed since the preview. Repeating preview and apply SHALL
converge to zero pending pages.

#### Scenario: Preview writes nothing

- **WHEN** the mode is previewed on a vault with variant tags
- **THEN** groups are listed with counts and no file changes

#### Scenario: Confirmed apply rewrites tags only

- **WHEN** a preview's `plan_id` is applied with a reason
- **THEN** the batch's pages carry the canonical tags
- **AND** their bodies are byte-identical to before
- **AND** a later preview reports no pending pages

#### Scenario: A stale plan is refused

- **WHEN** a planned page changes between preview and apply
- **THEN** apply fails with `STALE_TAG_VARIANT_PLAN` and writes nothing
