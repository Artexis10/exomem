# Supersession

Use supersession when a new page should replace an old page as the current answer. The active
engagement policy supplies the authority for that replacement; supersession is a restructure.

## Correct, supersede or archive

Read the affected claims and their evidence before you choose the writer.

- **Correct in place** when the page's subject and retrieval question still fit. Fix one claim with
  `edit_note` on its unit, or add a corrective unit with `add_observation`
  ([writing](writing.md#correcting-existing-knowledge)). Guarded edits keep the earlier text:
  `read_page(page="<page>", part="history")` shows it. Never write a replacement page only to keep
  old wording.
- **Supersede one claim** when a single claim is stale and the page still fits (see below).
- **Supersede the page** when another page should become the current answer.
- **Archive** when the page is no longer relevant and nothing replaces it: the topic was dropped or
  the project pivoted.

Refutation and contradiction do not imply supersession, and they do not lower a claim's rank. When
the history of a false claim matters, keep the claim and record its verdict.

## Supersede a page

Call `supersede_note(note="<old page>", title="<new title>", content="<body>", why="<why>", sources=["<source>"])`.
Pass `preview=true` first to see the draft without writing either page. One call writes the new
page, marks the old one superseded and links the two. The old page stays in place, unchanged in its
body, and links to it still resolve. Readers follow the chain to the new answer. A superseded page
takes no further edit: change the current page instead.

- Every entry in `sources` must resolve to a governed Source or Evidence page, even when the old page
  cited something unresolved. Capture the original first, or pass an honest empty list. Never
  promote a derivative into the missing original.
- Sources and Evidence cannot be superseded. Correct a Source through its correction route
  ([mutation results](mutation-results.md#files)).
- Never delete a page that a newer page replaces. The old page keeps the causal history and keeps
  citations from other pages working.

## Supersede one claim

A stale claim inside a page, an entity page included, supersedes at unit level without a new page.

1. Read the unit ids with `read_page(page="<page>", part="units")`.
2. Call `edit_note(note="<page>", action="supersede_observation", unit="<unit id>", text="<replacement>", why="<why>")`.
   For a claim that is still plain list text, pass its exact text as `span` instead of `unit`.
3. Expect one new unit that supersedes the old one. The old unit's text stays, and recall, search
   and the graph follow the link.

When superseding a compact claim, omit `kind` to use the shipped replacement kind. Pass a governed
rich kind to choose another.

Two actions on the same claim keep its text. `action="assess_observation"` records a `verdict`, with
an optional `check_by` date and evidence `links`. `action="link_observation"` adds typed `links`;
it never removes one. Assessing or linking a compact claim requires a governed rich kind.

## Downstream pages

After a supersession, the pages that cited the old answer may need attention. List them with
`inspect_memory(operation="connections.inbound_links", arguments={"target": "<old page>"})`.
Propose updates to the ones whose claims depend on the old answer, and let the user decide whether
to supersede them too. Never cascade a supersession on your own.

## Archive a page

1. Move the page into an `_archive` folder beside it with `files.move` ([tools](tools.md)).
2. Set its status to archived with `edit_note(note="<page>", action="set_fields", fields={"status": "archived"}, why="<why>")`.

An archived page carries no `superseded_by` and stays searchable. Audit checks such as orphan
detection skip it.
