# Bound search passages by characters

## Why

A search hit on a Source carries `passage`: the matched chunk, or `MAX_WORDS_PER_CHUNK` words around the first keyword match, plus a short context window. The bound counts words. A compact JSON log has one record per line and no spaces, so one "word" is a whole record. A passage of 350 such words ran to about 174 KB. One compact `search_memory` call with a limit of 6 returned 534 KB, and an agent cannot use that result.

## What Changes

- A passage shows at most `MAX_PASSAGE_CHARS` (4,000) characters. Most 350-word chunks of prose fit inside it with both context windows. Code and text without word breaks can pass it.
- A longer passage is cut to that length. The cut keeps the first place where the query matches in the passage and opens one context window before it, so the passage still shows the match. Without a literal match, the cut keeps the start of the chunk.
- `offset` and `length` describe the passage as shown, so `read_memory` at `passage.offset` still opens it and returns the rest.

## Impact

- Affected capability: `find-recall-efficiency` (the requirement "A hit on a long Source is one hit with its matched passage").
- Code: `src/exomem/find_parts.py`. No tool parameter, description or output schema changes.
