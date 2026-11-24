## Context

The CLIP chance band (`search-lane-relevance`) keeps noise-level image matches out of mixed results. It measures the chance level over the whole index on purpose, so a filter does not change what an unrelated match scores. That reasoning holds while text pages compete with images. Inside a restriction to pictures, nothing competes: the band can only withhold the image the person means, when the query describes it weakly.

Sources consulted: Anthropic, "Writing effective tools for agents" (2025), for parameter design, where one optional, plainly described parameter beats a filter expression the agent must compose; and Anthropic, "Demystifying evals for AI agents", for deciding the change on a held-out set with per-case intervals.

## Decisions

### D1. Reuse `page.file_type`; add no filter field

`page.file_type` is already an indexed scalar axis, filled from the sidecar's `media_type`, and the leaf's `file_types` shortcut compiles to it. A new `page.media_type` field would duplicate it. The public parameter exposes the shortcut under the same name.

### D2. The restriction is read from the eligible set, not the filter expression

The lane treats a search as restricted to pictures when the caller's filters are active and every eligible page is the sidecar of an image or video. Reading the eligible set covers every spelling: `file_types`, `filters` with `$eq` or `$in`, and combinations with other axes. "Image or video" comes from the extension registry in `media_types` (`VISUAL_KINDS`, the kinds the CLIP index embeds). That set is closed and fixed by the extraction schema.

Cost: one suffix check per eligible page. At 20,000 eligible pages it measured 3.6 ms in the worst case, where every page is a picture. The skipped band saves 2.7 ms at 100,000 CLIP rows in that same case. A mixed set stops at its first non-picture page.

### D3. Band off and a dedicated CLIP weight inside the restriction

Switching the band off alone did not help in a dev replay. With the band off and equal weights, fused ranking found 13 of 21 targets, the same as today: the text lanes ranked by tags and OCR noise. CLIP order found 17 of 21. The CLIP weight inside a restriction to pictures is therefore a ranking setting, `clip_media_restricted_weight`, not a code path that throws the text lanes away. Text-bearing images such as screenshots need the text lanes. The dev sweep over photos and text-bearing images sets the default. A large weight reproduces CLIP order with text breaking ties; 1.0 reproduces equal fusion.

### D4. The tool description carries the intent

No vocabulary, regex or classifier inside Exomem decides that a question is visual. The parameter's description tells the agent when to set it. When the agent is unsure that a picture is meant, the description tells it to search both with and without the restriction, so an unclear case is never worse than an ordinary search. A real-model check confirms that an agent sets it in conversations about pictures and leaves it unset in text conversations.

## Risks

- An agent sets `file_types` on a text question. The search then returns only pictures. The held-out guard measures how often this happens.
- A restriction to pictures that a caller forces with `rerank` still scores sidecar text. Auto rerank is off on the service, so the default path is unaffected.
