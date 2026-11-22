# Design

## Context

The normal search index already has chunks for long Sources. Paragraph splitting can flatten timed lines, page ranking can discard a competitive Source, and the hit can lose the chunk's location. File ingestion already stages and verifies exact bytes.

## Decisions

1. Reuse the embedding chunk path and the existing passage response. A passage is an address in authenticated Source text under the published hash. Semantic unit identity remains the authored anchor or fingerprint-bound reference defined in canonical store design D10.
2. Use the existing lexical and vector ranks to select the highest verified transcript among the neutral top three. Apply the reorder after scoring and before the public cap. This follows the context-quality row of the agentic engineering roster. Return the smallest citable evidence that answers the question.
3. Refresh installed governed catalogs through the current projector transition. Reuse existing page vectors when their input is unchanged. Bind each bank digest in the projection measurement. Link unchanged banks across private namespaces so their bytes follow namespace collection. Read banks only for admitted Sources in the neutral top three. Long turns use bounded text slices that retain the turn timestamp. Build governed passage banks from the first extraction part and report multipart coverage from the Source's own declaration. This keeps bank coverage independent of the shared catalog budget. The incremental index records the verified layout beside each transcript. Replacing that transcript's chunks clears the record, including when an older application writes them.
4. Extend the existing staged file capture boundary. Use a scoped canonical receipt with a bounded exact lookup. Stage bytes and verify their digest before acquiring the mutation boundary. Persist the receipt under that boundary alongside creation or authorized duplicate reuse. An import ID binds one standalone Source. Expanding a container can create additional Sources, so that operation uses ordinary capture. Reuse under an import ID requires a Source outside whole-Source erasure custody. This prevents a durable pointer that the existing eraser cannot account for.

## Risks and Trade-offs

A ranking lift can crowd out a summary. Compare the same cases with and without the change, including ordinary summary queries. An old or withheld chunk can disclose words and position. Verify the winning chunk against the caller's released Source before it influences the order. A lost write reply can otherwise create another Source. Resolve it through the durable receipt.

A later missing part leaves earlier citations readable. The Source read reports an unknown total and keeps the gap visible at the next offset. Search adds no reads to prove later parts.
