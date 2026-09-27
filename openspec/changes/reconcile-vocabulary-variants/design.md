## Context

Tags are cleaned in four writer modules (`note`, `add`, `edit`, `link`), each lowercasing and
mapping `_`/space to `-`. The lexical catalogue stores each page's tags as a casefolded JSON
members array (`pages.tags_json`). Post-commit vocabulary delivery
(`vocabulary_delivery.after_commit`) owns the single `vocabulary_advisory` slot for relation
review notices. Maintenance modes follow two confirmation patterns; `structured-files` is a
read-only preview plus exact-plan apply and is admitted on remote surfaces.

## Decisions

**One fold, one module.** `vocabulary_fold.fold_term(text) -> str` plus a frozen
`EXCEPTIONS` set is the whole shared contract, so a parallel change that needs the same fold
for claims can import it and the later merge only reconciles one small module. Grouping,
counts and canonical choice live in `tag_variants.py`, outside the shared contract.

**Conservative over complete.** The fold inflects only the final hyphen segment, only
alphabetic words of four or more letters, and never restores a silent `e`. It misses some
variants (`caching` does not meet `cache`) rather than merging different words. Meaning-bearing
forms (`news`, `series`, `analysis`, `status`, `process`, `windows`, `https`, `building`, …)
are listed as exceptions.

**Canonical is the most-used spelling.** Ties prefer the write-time normal form, then the
shorter, then lexical order. A variant is acted on only when its canonical has strictly more
uses.

**Write time reads a cached catalogue aggregate.** One `json_each` aggregate over
`pages.tags_json` costs about 60 ms at 5,000 distinct tags and 8,000 pages; the folded index is
cached per vault for 120 seconds, bringing a write's lookup to well under a millisecond. An
absent or stale catalogue fails open: no rewrite, no advisory.

**The advisory reuses the existing slot.** A tag notice has its own family
(`tag-variant/v1`) and a closed shape validated before public projection. A relation review
notice is evidence-bound work and keeps the slot when both apply; the tag variant remains
discoverable through the maintenance route.

**Maintenance is plan-gated and batched.** The preview walks compiled pages (not Sources,
Evidence or infrastructure), computes the variant mapping, and plans at most 64 pages. The
`plan_id` hashes the mapping and each planned page's content hash, so an apply against a
changed vault is refused as stale. Apply rewrites under the writer's mutation guard with
per-page path guards and expected hashes, splices only the frontmatter `tags` key, verifies
the body is byte-identical and the tags parse back as planned, and records one log entry. A
re-run converges: an already-applied vault previews zero pending pages. Pages whose tags key
cannot be swapped safely are reported as `unrewritable` and never planned, so they cannot pin
later batches. Like `structured-files`, the mode is admitted on remote surfaces because apply
requires the exact reviewed plan.

**Visible failure.** `_project` keeps closed projection reasons and `public_projection`
releases a reason only from a closed allow-list, falling back to `guidance_unavailable`.
Swallowed exceptions log the exception class and the innermost `module:line`, never the
message, which may quote page text.

## Risks

- A fold exception list is never complete; a meaning-changing merge is limited to groups the
  owner confirms in maintenance, or to `maximal` writes, which the owner opted into.
- The cached index can lag by up to two minutes after a large retag; canonical choice rarely
  moves, and maintenance reads pages directly.
