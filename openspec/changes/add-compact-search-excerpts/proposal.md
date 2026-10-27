# Add Compact Search Excerpts

## Why

`search_memory` returns compact hits by default. A compact page hit carries only path, title, type, scope and
updated date, so an agent cannot tell the page that holds a fact from its sibling pages. In a blind evaluation
on a replayed real vault, agents chose pages by title, opened recap and sibling notes, missed the page that held
the needed fact, and used up their call budget. Semantic-unit hits already carry a short `excerpt`; page hits do
not.

## What Changes

- A compact page hit carries `excerpt` when the hit has one. The excerpt is cut to at most 240 characters at a
  word boundary, followed by a trailing ellipsis marker when it was cut.
- A compact page hit has no `excerpt` field when the hit has no excerpt.
- The release plane applies to the compact excerpt exactly as it applies to the full excerpt. The cut happens
  after every release strip, so the compact excerpt, without its marker, is a prefix of the whitespace-collapsed
  excerpt that full mode releases for the same hit.
- Release stripping also trims an excerpt that a length cut left inside a link or path to a stripped source,
  at either end. Page, unit and matched-unit excerpts, and the truncated body of a bounded read, all pass through
  that one step, in compact and full mode.
  Before, such a fragment survived the strip because only whole identities matched, and compact mode moves
  every default search onto that path.
- Compact hits still omit ranking `signals`. Full mode and its default are unchanged. No parameter, tool
  description or output schema changes: the published `PageHit` shape already declares an optional `excerpt`.
- The retrieve-inject hook keeps its contract. It reads only `path`, `type` and `updated` from a compact hit and
  never shows an excerpt; the spec wording stops claiming that compact hits have no excerpt.

## Impact

- Affected capabilities: `find-recall-efficiency`, `retrieve-inject-hook` (wording only),
  `retrieval-explainability` (the compact explanation scenario no longer says that excerpts are absent).
- Cost: each compact page hit grows by 135 to 229 bytes as measured in independent review; the ceiling is about
  257 bytes of ASCII (240 characters, the marker and the JSON key).
- Privacy: the compact excerpt reuses the existing projection in `governance/egress.py`. It adds no second copy of
  the release rules.
