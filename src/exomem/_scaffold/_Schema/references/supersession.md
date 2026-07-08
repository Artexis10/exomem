# Supersession Protocol

When information in the Knowledge Base needs to be replaced, **supersede; do not
delete or rewrite in place**. Supersession preserves causal history (why is the
current page the way it is?) and keeps the audit trail intact.

## User-facing trigger

When a user says "this replaces the old conclusion," "the old recommendation is
wrong now," or "make the new version the current answer," use supersession. Do
not create a parallel note unless the old page is not actually being replaced.
Report it in user language: "Saved the new conclusion and marked the old one as
superseded."
## When to supersede vs. edit

**Edit in place** (small changes; same `updated` field bump):
- Typo, formatting, link fix
- Adding a new source citation to an existing claim
- Adding an `## Open thread` or extending `## Connections`
- Reorganizing sections without changing the substantive claim

**Supersede** (substantive change):
- The core claim, finding, or recommendation has changed
- The page's structure has changed enough that diff history is hard to follow
- A new source has shifted the conclusion
- The note has accumulated cruft and needs a clean restatement

When in doubt, supersede. It's recoverable; in-place rewrites are not.

## How supersession works

1. **The new page is authored** under the same path and naming convention. Choose
   a slightly different filename to avoid collision: append `-v2`, `-revised`, or
   a more descriptive variant.
   - `agentic-rag-retrieval-budget.md` → `agentic-rag-retrieval-budget-v2.md`

2. **The new page's frontmatter sets `supersedes`** to the old page's wikilink:

   ```yaml
   ---
   type: research-note
   project: project-alpha
   status: active
   created: 2026-05-09
   updated: 2026-05-09
   sources: [...]
   supersedes: "[[Knowledge Base/Notes/Research/Project Alpha/agentic-rag-retrieval-budget]]"
   tags: [...]
   ---
   ```

3. **The old page's frontmatter is updated:**
   - `status` → `superseded`
   - `superseded_by` → wikilink to the new page
   - `updated` → today's date

4. **The old page's body gets a header banner** (above any other content):

   ```markdown
   > [!warning] Superseded
   > This page has been replaced by [[Knowledge Base/Notes/Research/Project Alpha/agentic-rag-retrieval-budget-v2]] on 2026-05-09.
   > Reason: <one-line reason>
   ```

   Obsidian (and other callout-aware viewers) render `> [!warning]` as a callout; in
   plain markdown it is an ordinary blockquote, which is fine — the callout is an
   optional nicety. The explicit "Superseded" header is
   required.

5. **The old page is NOT moved or deleted.** It stays in place. Backlinks to it
   still work; they now lead readers to a clearly-marked superseded page that
   points forward.

6. **Both index.md files are updated** — the new page is added; the old page's
   entry is annotated `(superseded)`.

## Reasons to keep superseded pages

- **Causal history.** Future-you reading the new page may want to know why it's
  different. The old page tells you.
- **Quoted elsewhere.** If a third page cited the old page's claim, that citation
  should still resolve to something — even if that something is "this was the old
  answer; here's the new one."
- **Audit trail.** Git preserves old versions, but doesn't make the supersession
  relationship discoverable from inside your vault viewer (e.g. Obsidian). The link does.

## Cascading supersession

When a research note is superseded, the insights and patterns it fed may also
need attention:

- The skill checks `ingested_into` on the new page's sources to see what else
  cites them.
- The skill flags downstream pages whose `sources` include the now-superseded
  page, and proposes (does not perform) updates to those pages.
- You decide whether to also supersede the downstream pages.

The skill never cascades automatically. It only flags.

## Archival vs. supersession

Sometimes a page is no longer relevant but isn't being replaced — the topic has
been dropped, the project has pivoted, the question is no longer interesting.
That's archival, not supersession.

To archive:

- Move the page to an `_archive/` subfolder of its current location:
  `Notes/Research/Project Alpha/_archive/dropped-rag-experiment.md`
- Set `status: archived`
- No `superseded_by` field
- Update the relevant `index.md` — archived pages are listed in a separate
  "Archived" section at the bottom

Archived pages remain searchable by **find** but are excluded from default audit
checks (orphan detection, etc.).

## What never happens

- Deleting a page (use archival instead)
- Editing a superseded page's body content (only the supersession banner and frontmatter are added)
- Creating a `superseded` page without a `superseded_by` pointer
- Creating a page with `supersedes` pointing to a page that hasn't been marked `superseded`

These conditions are checked by audit. Violations are reported.
