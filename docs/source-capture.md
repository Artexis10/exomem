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
    "name": "capture_source",
    "arguments": {
      "title": "Captured record",
      "content": "Original text",
      "source_kind": "session",
      "source_identity": {
        "system": "archive",
        "record_id": "item-4",
        "revision": "r2"
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

The original capture digest covers text, trimmed title, resolved kind and domain, URL, normalized tags, reason, projects, requested slug and raw protection.
It excludes invocation identifiers, response detail, compilation hints, timestamps, allocated paths and server identifiers.
The initial atomic Source write stores identity and `capture_input_digest`; later metadata and back-reference updates preserve both.

Conflicting original inputs return `SOURCE_IDENTITY_CONFLICT` without a capture write.
Files or adoption with source identity return `SOURCE_IDENTITY_TEXT_ONLY` before retrieval.
The local `exomem attach` command supports `--lane source` for file capture without text identity.
An unreadable or malformed canonical Source candidate returns `SOURCE_IDENTITY_UNAVAILABLE`; indexes cannot prove capture absence.
The scan checks canonical pages in Sources and Evidence. Verified companion bindings distinguish Markdown originals from canonical pages.
An original's frontmatter cannot supply a capture receipt or classify another page.

Identity-enabled capture requires existing whole-namespace content authority and the existing creation boundary.
The server rechecks authority before commit and returns `SOURCE_IDENTITY_FORBIDDEN` to restricted callers without disclosing existence.
The same authority check precedes cached replay and invocation-key mismatch checks.
Restricted callers can continue ordinary capture under its existing authority rules.
Existing Sources need no backfill, and omitting identity preserves existing capture behavior.
