## Why

`adoption_studio(action="finish")` always returns a first question and its search route, even when its own recall check is pending or failed. After a large import the search index often has not caught up at finish, so the question points at a page that the search cannot return yet. The recall check's reconcile route also names a tool that the product surface does not list.

Import also drops a note's inline `#tags`. `plan` reads tags only from front matter, and `finish` reports that gap as a known limit. A collection that tags in the body loses those tags on its Sources and its own notes.

## What Changes

- `finish` returns `first_question` and its search route only when the recall check finds the imported page. Otherwise it returns `indexing`: the recall check's state (`pending`, `failed` or `missed`) and the recall sample's counts.
- `finish` spells every route it returns for the serving surface, as the handoff spells its calls.
- `plan` records a text item's tags as its front-matter tags followed by its body's inline `#tags`, read with the shared Markdown parser, which skips code, comments and escapes. The Source copy and the own note carry those tags.
- `finish` no longer reports the inline-tag limit.

## Impact

- Affected specs: `adoption-studio`.
- Affected code: `src/exomem/adoption_run.py`, `src/exomem/adoption_plan.py`.
- This change modifies "Own notes adopt as governed draft notes" after `batch-own-note-import-commits`, so it archives after that change.
