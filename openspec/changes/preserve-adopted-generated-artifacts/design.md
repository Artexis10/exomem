## Context

`preserve_artifacts` already stages hostile client handles before the mutation lock, streams and hashes exact bytes, commits append-only through `preserve_stream`, and returns a bounded receipt. The missing product transition is semantic: active agents are not told that generated variants stay ephemeral until the user adopts one, nor that the adopted bytes and a later delivery event are separate facts. As a result, a final output can be lost or reduced to prose even though the binary path already works.

The design must work for any generated file type, retain Source-versus-Evidence semantics, avoid collecting drafts, and be honest on Hosted surfaces that do not expose direct file handles. Hosted v1-v4 packages remain immutable.

## Goals / Non-Goals

**Goals:**

- Preserve exactly one explicitly adopted offered artifact and prove its byte identity.
- Keep unselected, rejected, revised-away, and abandoned drafts out of canonical storage.
- Make adoption durable and idempotent across retries and later sessions.
- Record external delivery separately and avoid unproven remote-byte claims.
- Extend the existing byte-preservation leaf rather than create a parallel blob path.
- Teach direct-handle and honest handoff behavior through the single shared immutable Hosted v5 candidate.

**Non-Goals:**

- Automatically preserving every generated result or guessing finality from filename, MIME type, quality, or generation completion.
- Reconstructing binaries from descriptions, OCR, base64 emitted through a model, or re-encoding.
- Claiming an upload, publication, send, or remote byte identity before evidence proves it.
- Backfilling historical Evidence with manufactured adoption or delivery state.
- Creating a Records collection when no compatible collection exists.

## Decisions

### D1. Adoption and delivery are two ordered facts

The lifecycle is `drafts -> explicit adoption -> exact-byte receipt -> optional observed delivery Record`. Selection or approval identifies the offered artifact. A later report that a uniquely identified offered artifact was sent or published can request the same sequence, but the agent must commit the local bytes first and must not turn the external report into a preservation receipt.

Alternative considered: treat sent or published as equivalent to saved. Rejected because it cannot prove local custody or remote byte identity.

### D2. Both exact-byte handle lanes gain one adoption envelope

The envelope carries a durable scoped adoption key, an explicit trigger, and one `selected_file_id` that must match exactly one supplied handle. It is accepted by `capture_source` for the Source lane and `preserve_artifacts` for the Evidence lane. When present, only that handle is staged for canonical commit; siblings receive no canonical write. Ordinary calls without the envelope keep their current semantics.

The trigger vocabulary records an explicit user-visible fact, not a semantic classifier result. Validation occurs before canonical publication, while staging and hashing remain outside the mutation guard.

### D3. Each lane's canonical companion owns the portable adoption receipt

The canonical page written atomically with every artifact gains a versioned adoption block containing a digest of the scoped adoption key, trigger, selected file identifier, lane and destination, artifact path, SHA-256 algorithm and digest, byte size, content type, and media identity. For Evidence this is the existing Evidence companion; for Source it is the Source artifact page required by attachment ingestion. The response projects those committed fields with `committed=true`. A derived lookup may index receipt blocks, but the vault-owned page is authoritative and portable.

Alternative considered: keep the only durable receipt in runtime SQLite. Rejected because it would separate provenance from the user-owned artifact and break portability. Alternative considered: create an unrelated receipt note. Rejected because the existing atomic artifact-plus-companion write set already supplies the correct custody boundary.

### D4. Request replay and durable adoption replay prove identity differently

Transport `Idempotency-Key` covers the ordinary request replay window: a matching cached terminal request returns without a second fetch or canonical write. After that window, the scoped adoption key remains durable, but the server MUST re-stage and hash the presented handle before it can compare the selected identifier, trigger, lane, destination, hash, size, content type, and media identity with the vault-owned receipt. Exact identity returns the original receipt without rewriting canonical data. Any mismatch fails closed as `ADOPTION_KEY_REUSED`. If the bytes are expired or unavailable and identity cannot be reproved, the request fails `ADOPTION_REPLAY_UNVERIFIABLE`; it does not claim replay and writes nothing.

### D5. Semantic destination precedes transport

The active agent first decides whether the adopted bytes are reasoning input (`Source`) or proof-bearing output (`Evidence`), then chooses `capture_source` or `preserve_artifacts` and finally direct handle or upload handoff. Both direct-handle lanes share staging, byte verification, adoption identity, and receipt fields. MIME type never decides the semantic destination. A transfer capability or handoff preparation is non-committing and may never be projected as saved.

### D6. Delivery uses an existing compatible Records collection

After a local receipt exists, a definite sent, published, or delivered observation may be written through `record_memory` and linked through the collection's declared link field to the Evidence companion. Remote byte equality remains unverified unless a platform receipt or export provides matching identity. Without a compatible collection, the agent may propose one but performs no implicit schema mutation.

### D7. Artifact behavior participates in the shared v5 candidate

The single v5 candidate owned by `capture-durable-personal-baselines` preserves v4's command membership and order unless an actual gateway bridge changes the callable surface. Before that owner renders or locks v5, this change adds its artifact doctrine and candidate-scoped selection fixtures to the same candidate-owned core skill and combined fixture digest. It advertises direct preservation only when handles are available and otherwise reports a non-committing handoff requirement. This change cannot independently render, lock, promote, or roll back v5. V1-v4 bytes and identities do not change.

### D8. Adoption eligibility and write authority are separate

Selection, approval, send, or publication establishes that one offered artifact became durable work; it is not, by itself, an explicit command to write Exomem. Agent-initiated Source/Evidence adoption and a later compatible delivery Record are `proactive_capture` and obey its off/advisory/silent disposition. An explicit request to save or preserve remains an ordinary user-requested action. Proposing a missing collection is `structural_suggestions`; creating or changing a collection/schema is separate confirmed `restructure_execution`. No trigger creates standing delegation.

## Risks / Trade-offs

- [A guessed trigger preserves drafts] -> Require an explicit offered-file identifier and publish draft/no-selection mutants.
- [Wrong sibling lands] -> Bind receipt identity to selected file id and staged digest; test sibling absence.
- [Durable replay cannot inspect expired bytes] -> Fail `ADOPTION_REPLAY_UNVERIFIABLE` after the request window; never accept identifier equality as byte proof.
- [Companion reconciliation drops adoption fields] -> Make the adoption block part of the governed companion vocabulary and test every renderer/reconciler.
- [Delivery wording overclaims] -> Gate Records on a committed local receipt and represent remote byte verification explicitly.
- [Hosted package history drifts] -> Candidate-own v5 skills and hash-check v1-v4 source and generated artifacts.
- [Selection is mistaken for write consent] -> Keep adoption eligibility separate from the `proactive_capture` disposition and test off/advisory/silent behavior.

## Migration Plan

1. Add red-first selection, byte-identity, idempotency, atomicity, handoff, and delivery tests.
2. Extend both Source and Evidence handle inputs, companion/page rendering and parsing, lookup, receipts, schemas, and projections compatibly.
3. Update active-agent carriers and contribute doctrine and fixtures to the single owner-controlled v5 candidate before its first render.
4. The v5 owner promotes only after both changes' real supported-client traces and historical immutability gates pass.

Before the first shared-v5 lock, rollback removes failed artifact doctrine from the unpromoted candidate and rebuilds it. After promotion, v5 remains immutable and any correction ships in v6; selection may move away from v5. Stopping adoption envelopes leaves ordinary `capture_source`, `preserve_artifacts`, existing Source, and existing Evidence valid; no data backfill or destructive migration is required.

## Open Questions

- Which Hosted UI or gateway will eventually own a complete no-handle upload bridge is unresolved. Until it exists, the only correct result is `handoff_required` or `handoff_prepared`, never preservation success.
- Whether externally delivered correspondence should itself be preserved as additional Evidence remains case-specific; the safe default is no automatic second capture.
