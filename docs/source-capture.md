<!-- authority:non-specification -->

# Durable Source capture

Service callers can supply `source_identity` to reconcile a text capture after a lost response, restart or expired invocation replay.
The identity contains three nonempty opaque strings: `system`, `record_id` and `revision`.
The identity is local to the vault. Revisions remain separate Sources; capture does not infer their order or retire earlier material.

```json
{
  "jsonrpc": "2.0",
  "id": 17,
  "method": "tools/call",
  "params": {
    "name": "save_source",
    "arguments": {
      "title": "Captured record",
      "content": "Original text",
      "source_kind": "session",
      "options": {
        "source_identity": {
          "system": "archive",
          "record_id": "item-4",
          "revision": "r2"
        }
      }
    },
    "_meta": {"io.exomem/idempotency_key": "capture-attempt-17"}
  }
}
```

The optional namespaced MCP metadata key selects existing explicit invocation replay for mutations.
It is transport metadata, so it does not appear in tool arguments or REST schemas.
The server validates a supplied key as a nonempty string and scopes replay to the verified principal, vault and verified client.
Reusing that key with changed arguments returns `IDEMPOTENCY_KEY_REUSED` during the existing 24-hour retention.
Credential issuance keeps its existing replay exclusions. Calls without the metadata key keep existing implicit replay behavior.

Durable identity has no replay expiry. An initial capture returns `capture_outcome: created` with its Source path and stable reference.
The same identity and original normalized inputs return `capture_outcome: reused`, `mutated: false`, and that same Source reference.
A fresh invocation key can reconcile after explicit replay expires.
Different identities with identical content create separate Sources.
Explicit promotion into Evidence preserves recovery at the promoted page's path and the same stable reference.

A capture under erasure custody adds no line to the top index, the activity log or the Sources index.
Erasure custody covers a capture with an issued capture reservation, or one whose identity system the host erasure policy enables.
List those Sources with `inspect_memory` operation `sources.inventory`. Other captures keep their listing on those pages.

The original capture digest covers text, trimmed title, resolved kind and domain, URL, normalized tags, reason, projects, requested slug and raw protection.
It excludes invocation identifiers, response detail, compilation hints, timestamps, allocated paths and server identifiers.
The initial atomic Source write stores identity and `capture_input_digest`; later metadata and back-reference updates preserve both.

Conflicting original inputs return `SOURCE_IDENTITY_CONFLICT` without a capture write.
Files or adoption with source identity return `SOURCE_IDENTITY_TEXT_ONLY` before retrieval.
The local `exomem attach` command supports `--lane source` for file capture without text identity.
An unreadable or malformed canonical Source candidate returns `SOURCE_IDENTITY_UNAVAILABLE`; indexes cannot prove capture absence.
The scan checks canonical pages in Sources and Evidence. Verified companion bindings distinguish Markdown originals from canonical pages.
An original's frontmatter cannot supply a capture receipt or classify another page.
Canonical receipts remain authoritative at any Markdown filename, including `index.md`.
The scan excludes the frontmatter-free `Sources/index.md` catalog only when its `## By type` and `## Recent captures` sections remain present.
Catalog rows may be stale; they cannot prove capture absence.

Catalog recognition does not establish ownership.
Replacing all receipt metadata and bytes with a valid catalog makes the former receipt indistinguishable from a catalog.
Removing either fixed section can make identity reconciliation unavailable; ordinary capture retains its existing authority rules.

Identity-enabled capture requires existing whole-namespace content authority and the existing creation boundary.
The proof checks RAW authority, the connector ceiling and the current local policy.
A nonempty local policy must permit full disclosure across all declared scopes for the current audience and purpose, including standing grants.
This conservative proof also forbids identity reconciliation when an unrelated Notes restriction applies.
Exact-item session grants cannot prove whole-namespace access.

The server returns the same forbidden outcome for blocked policy, unavailable purpose authority or malformed verified session context.
The server rechecks authority before commit and returns `SOURCE_IDENTITY_FORBIDDEN` to restricted callers without disclosing existence.
The same authority check precedes cached replay and invocation-key mismatch checks.
Restricted callers can continue ordinary capture under its existing authority rules.
Existing Sources need no backfill, and omitting identity preserves existing capture behavior.

## Declared passages

A text capture can declare ordered records through `options.passages` instead of `content`.
Each record contains exactly `key` and `text`:

```json
{
  "title": "Harbour survey",
  "source_kind": "session",
  "options": {
    "source_identity": {"system": "archive", "record_id": "survey", "revision": "r3"},
    "passages": [
      {"key": "record-42", "text": "The lantern uses oil."},
      {"key": "record-43", "text": "The keeper records oil deliveries."}
    ]
  }
}
```

A capture accepts 1–2,000 passages. Keys must be unique within the capture and contain 1–128 UTF-8 bytes.
Text must be nonempty UTF-8, without edge whitespace, carriage returns or blank lines. Validation refuses invalid declarations before writing.
Exomem preserves the text exactly. Keys and text participate in identity reconciliation; a changed key conflicts with an existing identity.

The canonical Source body holds a JSON key block before its Capture section. Frontmatter holds only the passage count.
File mode and store mode use those same canonical bytes. Keys never contribute to lexical text, vectors or scores.
Each passage supplies its own chunks. A long passage splits at the existing chunk cap and retains one key and ordinal.

## Passage search

`search_memory` accepts `options.result_level: "passage"`:

```json
{
  "query": "lantern oil",
  "limit": 5,
  "tags": ["public"],
  "options": {"result_level": "passage"}
}
```

Passage search requires whole-namespace content authority. Restricted callers receive `PASSAGE_AUTHORITY_REQUIRED` and can use ordinary page search.
The service must first publish its corpus through the maintained warm-up. A cold or invalid publication returns `PASSAGE_INDEX_UNAVAILABLE`.
The request never builds the corpus. Filters and relations select eligible Sources before dense ranking.
Passage search accepts limits from 1 to 200 and refuses page-ranking options such as reranking, graph expansion or compilation boosts.

Each hit returns a Source `ref`, optional `source_identity`, `passage_key`, one-based `passage_ordinal`, `passage_hash`, `span` and dense `score`.
The span uses character offsets in the Source body after frontmatter. Its fields are `start` and `length`.
The hash covers the complete declared passage as UTF-8, including when a hit covers only one chunk.
Several passages from one Source can rank. Hits contain no passage text; callers resolve their own records through the opaque key.
The reply identifies the embedding `recall_space`, its opaque `recall_space_id` and the backend actually used in `scoring_mode`.
It counts `withheld_chunks` whose indexed text no longer matches their Source.
Exomem does not refill withheld hits. A request reads at most `limit` distinct selected Sources; terminal checks can reread those Sources.

`EXOMEM_PASSAGE_MATRIX_BYTES` configures the process-wide numerical ceiling for optional passage matrices and replacement scratch.
Its default is zero. A positive ceiling permits background loading after declared passages are published.
Current admitted matrices can serve resident scoring. Cold, lagging or capacity-denied state uses disk scoring without waiting for a matrix.
Reader-held matrices retain their charge after replacement or eviction.
The [passage reference](../benchmarks/passage-evidence/README.md) records the measured configuration and its limits.

## Cell discovery

A Cloud cell exposes `GET /.well-known/exomem-cell` without requiring a credential or tenant content.
Clients can compare its configured `cell_id` before sending credentials or content to that destination.
The response lists implemented operations and their effective limits from the composed command registry.
It identifies package files observed during server composition and the opaque published recall-space identity.
An unavailable recall space remains explicit; discovery does not load an encoder.
Discovery exposes no Source identity, content, credential or local model path.
