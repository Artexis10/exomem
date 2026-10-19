## Context

`op_list_directory` calls `list_directory.list_directory`, which takes the whole tree from `reserved_paths.list_generic_tree`, including each Markdown file's bytes. It parses frontmatter for every file, filters withheld entries in the leaf for a non-owner caller (`keep`), sorts, and returns everything. The dispatcher's terminal filter and the MCP layer's second pass then decide every returned path again.

## Goals / Non-Goals

**Goals:**
- Bound the work of one `list` call by the page size, not by the folder size.
- Keep the order, the entry shape and the withholding rules unchanged.

**Non-Goals:**
- Changing `overview`.
- Adding filters, a new sort order or a total count.
- Setting the browse latency bar. It stays pending in `bound-request-latency-across-surfaces` until installed measurements support an engineering choice.

## Decisions

1. **Page size.** `limit` defaults to 200 and is capped at 1,000. Smaller and larger values are clamped, never rejected. 200 entries keep a page readable by an agent client. The cap bounds the cost of one call.
2. **Cursor.** The cursor is an opaque URL-safe string that encodes three things: the listed path, the `recursive` flag, and the sort key of the last returned entry. The leaf resumes after that key. A cursor that does not decode, or that names another path or flag, fails with `INVALID_CURSOR`. The cursor carries only a path the caller already received, so it discloses nothing new. It is not signed, because a forged cursor can only start a listing at a different point of the same folder, which the caller may list anyway.
3. **No total count.** The leaf would need a withholding decision for every entry to count only the visible ones. A raw count would tell a non-owner caller how many entries are withheld. The response says only whether more entries follow.
4. **Names first.** `reserved_paths.list_generic_tree` gains an option that skips file bytes. The leaf sorts the names, walks them in order from the cursor, applies `keep` and the reserved-path and exclusion rules, and stops at `limit` visible entries. Then it reads bytes and frontmatter for those entries only, through the same descriptor-bound reads.
5. **One more probe.** The leaf collects up to `limit + 1` visible entries. `truncated` is true only when one more visible entry exists, so a final page that ends exactly at `limit` reports no further page.

## Risks / Trade-offs

- [A client expects a complete listing in one call] → The response says `truncated: true` and carries `next_cursor`. Clients that cannot send `cursor` yet still get the first bounded page.
- [The folder changes between pages] → A page resumes after the last returned key. An entry added before that key is not listed. An entry removed after it is not returned. Each page is correct for the folder as it is when that page runs.
- [A withheld folder's existence check walks its subtree] → `_withheld_target` runs only for the directory entries the page visits, not for every child.
