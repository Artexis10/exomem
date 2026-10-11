## Why

Exomem's differentiator is bitemporal reasoning — world time × knowledge time — but it
records its own knowledge time to the **day**. `created`, `updated`, and every `log.md`
history heading came from `dt.date.today().isoformat()`, so a note created and revised twice
inside two hours carried three identical dates and `created == updated`. The only ordering
signal was position in the file, not data. "Has this page changed since I read it?" and "in
what order did I reach these conclusions?" were unanswerable within a day.

Neither test suite could catch it: the memory-proof benchmark models knowledge time in weeks,
so it is structurally blind to sub-day ordering.

Investigating the read path surfaced a second, independent defect already shipping. `page.updated`
is strictly typed in the filter layer — `date` and `datetime` are deliberately not comparable —
and `page_view` passed the raw YAML value straight through. Because `serialize_frontmatter`
quotes any scalar YAML would re-type, `create_file`, `set_frontmatter_field`, and `multi_edit`
have always written `updated: "2026-05-24"`, which loads as `str`. Measured against the live
module, of the four spellings that reach the vault only the bare unquoted date matched a date
filter. Those pages are silently absent from every `recency_days`, `updated_after`, and
`updated_before` search — no warning, no error.

## What Changes

- Record `created`, `updated`, `captured`, and `log.md` history headings at **second
  granularity in UTC** (`2026-08-05T09:12:33Z`).
- **Do not backfill.** Existing date-only values stay date-only permanently. Rewriting them as
  midnight would assert a precision that was never captured. The mixed vault is the end state,
  not a migration window.
- Make precision part of the data: a date-only value denotes an unknown instant within its day,
  so ordering is four-valued (`before | after | same | indeterminate`) and never collapses an
  unknown into a guess.
- Add one shared `temporal` module. There was no shared clock helper: `_now_iso` was
  copy-pasted verbatim in two modules and reimplemented with four different precision and
  suffix conventions in five more.
- Carry precision on the Python type so the change is cheap and honest: `datetime` subclasses
  `date`, so the existing injectable `today` seam accepts either, and the ~394 call sites that
  pass a plain `date` keep getting day-granular output. No mass test migration.
- Fix the read paths so a value's YAML spelling stops deciding whether its page is searchable.
- Keep note paths (`YYYY-MM-<slug>`) and draft-token render dates day-granular, so filenames do
  not shift month for late-evening writes and `semantic_writes` needs no token version bump.

## Capabilities

### New Capabilities
- `note-knowledge-time`: Defines the recorded precision of `created`/`updated`/`captured` and
  history headings, the permanent mixed-format contract, and the four-valued ordering that
  follows from it.

## Impact

- Every governed write tool stamps an instant: `note`, `edit`, `replace`, `link`, `create_file`,
  `multi_edit`, `set_frontmatter_field`, `observe_memory`, `add`, `preserve`.
- `find` date filters begin returning pages that were previously dropped. This is a behaviour
  change to search results and is the point of the fix.
- `semantic_writes.DraftToken` gains `render_stamp` and `_TOKEN_VERSION` goes to 2. **Draft
  tokens in flight across the release boundary are rejected** and must be re-validated once; a
  v1 token carries no frozen instant, and inferring one at commit is the non-determinism the
  freeze exists to prevent.
- No vault migration, no reindex. Every pre-existing value already rendered as `YYYY-MM-DD`
  through `ParsedPage.updated`, so persisted index bytes are unchanged.
