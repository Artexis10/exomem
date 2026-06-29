# Design — Reasoning-ready context packs from `find`

## Context

`find_module.find(vault_root, *, query, ..., limit, ...) -> list[Hit]` is the read path.
A `Hit` carries `path, type, scope, title, updated, excerpt` + ranking signals — an
*excerpt*, not the note. The leaf `op_find` (in `commands.py`) wraps it, logs the call,
and returns `[h.as_dict() for h in hits]`; the registry derives the tool's params from
`op_find`'s signature + Google-style docstring and exposes it on MCP/REST/CLI.

The assembly primitives already exist and are all measurement-only:

- `find._parse_page` / `find._CACHE.get(path, vault_root)` → `ParsedPage` (frontmatter +
  body + `.title`/`.page_type`/`.status`/`.superseded_by`).
- `find._outbound_wikilink_paths(page, vault_root)` → resolved outbound wikilink targets
  (KB-scoped, `.md`, code-fence-aware).
- `vault.find_inbound_wikilinks(vault_root, rel_path)` → `InboundLink[]` (who links here).
- `corpus_aware._best_cosine_per_file(vault_root, *, title, body, k)` →
  `{file_path: best_cosine}` over the sidecar; `_canon()` for path comparison; band edges
  `_contradiction_floor()` / `_dup_threshold()` (env-overridable, default `[0.82, 0.90)`).
  All no-op to `{}`/`[]` when `KB_MCP_DISABLE_EMBEDDINGS` is set.
- `attention.py` is the precedent for "assemble measurement sources into one bounded,
  explicitly-truncated response."

## The composition

`assemble_pack(vault_root, hits, *, max_hits=None, max_neighbors=None, max_tension=None)`
in a new `context_pack.py`, called by `op_find` only when `pack=true`:

1. Take the top `min(len(hits), PACK_MAX_HITS)` hits as `packed`; load each `ParsedPage`
   once via `_CACHE` (reused across all four parts).
2. **claims** — `{rel_path: {title, type, lede, sections[], outline[]}}` via
   `_extract_claims(page)`.
3. **neighborhood** — union of inbound + outbound 1-hop neighbours of the packed notes,
   minus the packed set, ranked by co-citation, capped.
4. **contradictions** — `{superseded: [...], tension: [...]}`.
5. Assemble `{packed_paths, claims, neighborhood, contradictions, embeddings_available,
   truncation}`.

`find()` itself is **not** modified — the `pack` param and the `{"hits","pack"}` return
live in `op_find`, so the ranker contract and its tests are untouched and the list↔dict
polymorphism is confined to the tool boundary (REST returns JSON; the CLI already
pretty-prints a dict result via its `json.dumps` branch).

## Decisions

- **`pack` is a `find` parameter, not a new tool.** Claude flips it on the `find` it was
  already going to make — no extra always-loaded tool on the MCP surface (the surface-
  token cost is real and "consolidate over cut" is the standing principle), and it routes
  by natural language without Claude having to name a second tool. The cost is a
  polymorphic return; contained at the leaf, it is cheaper than a 23rd tool.
  - *Rejected — dedicated `context_pack` tool:* cleaner typing, but adds a tool to the
    always-loaded list and forces a tool-selection decision for what is "find, but with
    its context."
- **Key claims are extracted STRUCTURALLY, never generated.** This is the load-bearing
  pure-substrate decision. The lede is the note's own first paragraph; `sections` are the
  first line / leading bullets under recognized headline headings; `outline` is the `##`
  skeleton. The KB's compiled notes already carry this structure (Problem / Summary /
  Connections …), so structural extraction is high-signal **and** defensible — the server
  never decides what a note "means," it only quotes what the note says.
  - *Rejected — LLM/extractive summarization:* a server-side model picking salient
    sentences is exactly the brain's job and the out-of-bounds step.
- **Heading + lede scanning is code-fence aware.** A `#` or `[[...]]` inside a fenced
  code block is not a heading/link; the scanner tracks fence state (mirroring
  `find_body_wikilinks`) so code samples don't pollute claims.
- **Neighbourhood ranks by co-citation, not raw degree.** A neighbour linked by *2 of
  the packed notes* is more central to *this* result set than one linked by 1, so the
  primary sort key is `len(referenced_by)` (distinct packed notes connected), then total
  link richness ("both" directions > one), then path. Direction (`in`/`out`/`both`) is
  recorded. Packed notes are excluded from their own neighbourhood. Each neighbour
  carries only a one-sentence lede (token bound) — the pack points at neighbours, it does
  not inline them whole.
- **Contradictions are two measured kinds.** *Recorded supersession* edges (one note in
  the set whose `superseded_by`/`status:superseded` points at another note in the set)
  are definite, read straight from frontmatter. *Proximity tension* pairs are computed by
  embedding each packed note once (`_best_cosine_per_file`), keeping only pairs **among
  the packed set** whose cosine lands in the existing `[floor, dup)` band, and are framed
  verbatim as the contradiction queue frames them — "proximity, not polarity — reader
  decides." Reusing the same band + helper keeps one definition of "tension" in the
  codebase. The top-k of `_best_cosine_per_file` is ample for the band (band membership
  requires high similarity, so a band-neighbour is always in the top results).
- **`embeddings_available` is honest.** It is `true` iff at least one
  `_best_cosine_per_file` pass returned a non-empty map (a packed note always retrieves
  at least itself when the sidecar works). When embeddings are disabled/unimportable it
  is `false`, `tension` is empty, and the claims + neighbourhood + supersession parts
  still produce a useful pack — the same soft-fail contract `detect_contradictions` has.
- **Bounded with explicit truncation, env-overridable.** `KB_MCP_PACK_MAX_HITS` (5),
  `KB_MCP_PACK_MAX_NEIGHBORS` (10), `KB_MCP_PACK_MAX_TENSION` (10),
  `KB_MCP_PACK_CLAIM_CHARS` (280) — resolved at call time (so tests can monkeypatch),
  overridable per call via kwargs. Every cap that drops content appends a line to
  `truncation`; nothing is silently dropped (the `attention` / "no silent caps"
  discipline).

## Pure-substrate justification

Every field of the pack is one of: text copied verbatim from a note (lede, section
lines, outline headings, neighbour ledes), a wikilink edge the user authored
(neighbourhood, supersession), or rank/band arithmetic over cosines the sidecar already
computed (co-citation order, tension membership). No note content is summarized,
paraphrased, classified, or judged; no generative or reasoning model runs at pack time;
the vault is not mutated and `find` ordering is unchanged. This is the same status as
`find`'s RRF, the contradiction queue's band, and the `attention` surface — all in
bounds. A server-side LLM deciding which notes "really" conflict, or writing a synthesis
across them, would be the out-of-bounds step; the pack stops at assembly and hands the
reasoning to the brain.

## Risks

- **Schema-fidelity fixture must be regenerated.** Adding `find`'s `pack` param breaks
  `tests/test_mcp_schema_fidelity.py` until `tests/fixtures/mcp_tool_schemas.json` is
  regenerated via the existing `scripts/dump-tool-schemas.py`; the diff must add only the
  `pack` property to `find` and change no other tool.
- **Polymorphic `find` return.** `pack=true` returns a dict, not a list. Confined to
  `op_find`; default-off keeps every existing caller on the list path; REST/CLI already
  handle a dict result. Documented in the `find` docstring `Returns:` so Claude expects it.
- **Pack size.** An unbounded pack would defeat the one-shot goal; the caps + explicit
  `truncation` keep it bounded and honest. Defaults are conservative (top-5 hits).
- **Tool ambiguity.** `pack` is one boolean on an existing tool with a tight docstring
  ("assemble a reasoning-ready context pack from the top hits"), so it does not create a
  second tool to disambiguate.
- **Inbound-link cost (known follow-up).** The neighbourhood calls
  `vault.find_inbound_wikilinks` once per packed note, and each call walks the whole vault
  (twice — basename-uniqueness + a text read of every md). Bounded by `PACK_MAX_HITS`
  (default 5) and only on the opt-in `pack=true` path, but on a ~500-file vault that is
  several full walks of added latency. Accepted for v1; the follow-up is a single batched
  vault walk that resolves inbound links for all packed notes at once (or an mtime-keyed
  memo like the outbound `WikilinkResolver` cache already uses). Outbound is already cached.
