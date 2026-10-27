# Add Compact Search Excerpts

## Why

`search_memory` returns compact hits by default. A compact page hit carries only path, title, type, scope and
updated date, so an agent cannot tell the page that holds a fact from its sibling pages. In a blind evaluation
on a replayed real vault, agents chose pages by title, opened recap and sibling notes, missed the page that held
the needed fact, and used up their call budget. Semantic-unit hits already carry a short `excerpt`; page hits do
not.

## What Changes

- A compact page hit carries `excerpt` when the hit has one. The excerpt is cut to a fixed length at a word
  boundary, with a trailing ellipsis marker when it was cut.
- A compact page hit has no `excerpt` field when the hit has no excerpt.
- The release plane applies to the compact excerpt exactly as it applies to the full excerpt. The cut happens
  after every release strip, so the compact excerpt is never longer than, and is always a prefix of, the excerpt
  that full mode releases for the same hit.
- Compact hits still omit ranking `signals`. Full mode and its default are unchanged. No parameter, tool
  description or output schema changes: the published `PageHit` shape already declares an optional `excerpt`.
- The retrieve-inject hook keeps its contract. It reads only `path`, `type` and `updated` from a compact hit and
  never shows an excerpt; the spec wording stops claiming that compact hits have no excerpt.

## Impact

- Affected capabilities: `find-recall-efficiency`, `retrieve-inject-hook` (wording only).
- Cost: each compact page hit grows by about its excerpt, at most 240 characters. Measured on the fixture vault,
  a default `search_memory` call of two to four hits grew by 270 to 700 bytes.
- Privacy: the compact excerpt reuses the existing projection in `governance/egress.py`. It adds no second copy of
  the release rules.
