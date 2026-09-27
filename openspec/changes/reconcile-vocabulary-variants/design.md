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

**Plurals and separators only.** The fold applies NFKC, casefolds, maps runs of `_`,
whitespace and `-` to one `-`, and removes one plural from the final hyphen segment of an
alphabetic word of four or more letters. It never removes `-ing` or `-ed`: `training` and
`trains`, `recording` and `records`, `embedded` and `embeddings` are different words, and one
fold is shared by tags, collection claims and routing, so a wrong merge silently joins two
concepts everywhere while a missed merge only leaves two spellings apart. Hyphens join the
separator run so a writer's per-character mapping (`x  y` becomes `x--y`) folds with the
original. A must-not-merge table and a must-merge table in `tests/test_vocabulary_fold.py` pin
the boundary, together with idempotence.

**One count source.** Tag usage is the lexical catalogue's per-page `page.tags` members
(`LexicalStore.tag_members_by_page`), read once and filtered before counting: pages the
current reader may not see (`egress.restricted_release_filter`) and pages in a tree another
subsystem owns (`curation.PROTECTED_TREES` — Sources, Evidence, Records, Planning, workflow
contracts, `_Schema`, `_Governance`, `_Adoption` — plus trash, archive, attachments and dot
directories, compared casefolded at any depth) never contribute. Write-time advice,
post-commit advisories and maintenance all read this one `usage`, so a count, a group or a
canonical choice reads for a restricted caller exactly as if withheld pages were absent. An
absent or stale catalogue gives no advice at write time and a typed
`TAG_USAGE_UNAVAILABLE` refusal from maintenance.

**Canonical is the most-used written form.** Within a fold group, pages are counted per
normal form writers produce, and the canonical is the most-used one (then shorter, then
lexical), so it is never a raw spelling such as `Machine_Learning`. A spelling that differs
from the canonical only by separator is that form's own spelling, not a competitor. When the
two most-used forms tie, the group is listed as `tied` and nothing in it is advised or
rewritten; a variant is acted on only when its canonical has strictly more uses.

**Write time advises, never rewrites.** No prominence level changes an authored tag. Hosted
surfaces default to `maximal` (`prominence.default_for_surface`), so rewriting there would act
on tags no owner chose to have rewritten. At `maximal` a write adds one warning line per
minority variant; `note`, `add`, `edit` and `link` surface it. At any non-`off` level
post-commit delivery can carry one `tag-variant/v1` advisory. The unrestricted reader's usage
index is cached per vault for 120 seconds, including an empty one; a restricted reader's is
computed per call and never cached.

**The advisory reuses the existing slot.** A tag notice has its own family
(`tag-variant/v1`) and a closed shape validated before public projection. A relation review
notice is evidence-bound work and keeps the slot when both apply; the tag variant remains
discoverable through the maintenance route.

**Maintenance is plan-gated and batched.** The preview reads the usage, reads only pages that
carry a spelling a decided group rewrites, and plans at most 64 pages. The `plan_id` hashes
every decided group's canonical and each planned page's content hash and tags. Apply plans
outside the mutation guard, refuses as stale when the plan differs from the preview, and under
the guard re-verifies only the batch: each page's content hash, each page's visibility to the
caller (a withheld page answers exactly as a changed one), and the decision of every group the
batch touches. The splice changes only the frontmatter `tags` key, quotes any tag YAML would
read as a null, boolean, number or date, keeps a trailing comment on the `tags:` line, and
refuses a page whose block list carries a comment, or whose body or any other frontmatter key
would not come through unchanged. Such pages are reported as `unrewritable` and never planned,
so they cannot pin later batches. The log entry is the rollback record: each page's path,
before and after tags and hashes, and the inverse mapping from canonical to the spellings it
replaced. When `log.md` cannot take that entry, apply refuses with
`TAG_VARIANT_AUDIT_UNAVAILABLE` and writes nothing, as structured-file migration does. A
re-run converges: an applied vault previews zero pending pages. Like `structured-files`, the
mode is admitted on remote surfaces because apply requires the exact reviewed plan.

**Visible failure.** `_project` keeps closed projection reasons and `public_projection`
releases a reason only from a closed allow-list, falling back to `guidance_unavailable`.
Swallowed exceptions log the exception class and the innermost `module:line`, never the
message, which may quote page text. This makes the live `guidance_unavailable` cause
observable; identifying it is an open task.

## Risks

- A fold exception list is never complete. Without `-ing`/`-ed` folding the remaining risk
  is a plural whose singular is a different word; such a merge only changes tags through a
  maintenance batch the owner confirms, never at write time.
- The catalogue can lag a recent write, so a just-written variant may be missed until it is
  indexed, and the cached write-time index can lag by up to two minutes. Maintenance reads
  every planned page directly and re-verifies the batch under the guard.
