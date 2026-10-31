## Context

`finish` runs one recall check on the largest imported title and a recall sample of up to ten items. The check's `ok` is true when the search returned the exact target page, false when the search answered without it, and null with a `status` of `pending` or `failed` when the search could not answer.

## Decisions

- `finish` offers the first question only when `ok` is true. A miss misleads the same way as a pending check: the question asks about a page that the search does not return. The `indexing` block replaces the question, so the caller sees why no question is ready and how much of the sample the search found.
- `finish` stores the search route and the reconcile route in the grouped spelling that the command registry runs. Each presentation of the run spells them through `capabilities.active_call_route`, as it already rebuilds the handoff, so a run finished on one surface and read on another names tools that the reader lists.
- `plan` passes the front-matter tags and the result of `markdown_regions.inline_tags` to the existing tag normalization in one list. The shared parser already skips code spans, fenced blocks, comments and escapes, and drops a token of digits only. The normalization keeps the first spelling of a tag and reports a rejected tag as before. The tags are part of the planned front matter, so they are part of `plan_id`.

## Not chosen

- Returning the first question with a "not ready" flag keeps a question that the caller can run before it works.
- Waiting inside `finish` for the index to catch up holds the call open for an unbounded time.
