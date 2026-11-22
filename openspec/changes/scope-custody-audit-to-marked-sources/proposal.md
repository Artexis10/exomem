## Why

A `maintenance.audit` call that asks only for `custody_source` cannot finish on a large vault. The audit parses and authorizes every page in the Knowledge Base before its stages start, although only Sources that record `custody_only: true` can produce a custody finding. On a vault of about 17,400 pages that parse takes about 79 s. Per-page authorization accounts for 59 s of it. The served request allowance is 50 s, so the corpus parse is truncated, the custody stage is skipped and coverage is `unknown` on every call. An agent cannot tell which Sources it kept for custody still wait for a note, and an acceptance check that needs complete custody coverage cannot pass.

#2995 fixed the same shape for `source_pointer_drift` by parsing only the append-only tree. That does not reach this category: Sources and Evidence hold about 14,100 of the 17,400 pages, so an append-only parse still exhausts the allowance.

## What Changes

- An audit that selects only `custody_source` chooses its pages from the frontmatter of append-only pages before it authorizes or parses any of them. It authorizes and parses only pages that record `custody_only: true`.
- The pages that a custody Source names, its derivatives in `ingested_into` and its predecessors in `supersedes`, are read only when the check asks for them, through the same admission as the corpus parse.
- The findings equal those of the same check over the full corpus. A page that the caller may not see still contributes nothing and still reports as missing when a Source names it.
- Any other selection, including `custody_source` combined with another category, keeps its existing corpus read.

## Capabilities

### Modified Capabilities

- `action-first-audit`: a custody-only audit reads only custody-marked Sources and the pages they name.

## Impact

- `src/exomem/audit.py`: the corpus read for a custody-only selection and the backlog lookup.
- Tests in the audit and custody suites. No API, schema, storage or vocabulary change.
