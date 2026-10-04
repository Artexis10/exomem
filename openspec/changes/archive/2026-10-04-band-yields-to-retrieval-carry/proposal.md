## Why

With semantic evidence on, a turn that brushes a page's rare name can resolve that page on `rare_term` + `vector_band`, even when the turn is about something else. The original band-yield repair made the English conversion fixture T6 match its lexical arm. Current packet evidence shows that the lexical arm itself carries unrelated oven gas/fan settings: shared conversion words are not a page request or a subject association. The approved refinement preserves the ordinary band-yield path but treats explicitly scoped subject/property requests separately. Tightening `rare_term` + `vector_band` for everyone is not an option: that pair supports multilingual named anchors.

## What Changes

- A band resolution is yieldable when the band is what resolved the turn (every resolved anchor carries `vector_band` and would be `partial` without it) and the anchor's rare word was written as an ordinary word: never with an initial capital, in a turn that writes some cased word with a capital (sentence-initial included) and some without. A turn typed entirely in lower case carries no such signal and never yields, which is a known limit. This is the existing `name_lower_case` signal that bare-name ambiguity already reads.
- On a yieldable turn the compiler asks the retrieval carry what the turn names. If the carry names one dominant page that is not the band's page, and a carried packet builds from it, that packet is served, the same one the turn gets with semantic evidence off.
- Outside the scoped-request exception, the band's resolution stands in every other case: a capitalised name or a name in a script with no case, the carry naming the same page, several pages or none, or a carried page the lanes read nothing off. An unscoped yield never leaves the turn with nothing, and a phrase an unrelated note shares with the turn never overrules a rare name.
- Exception: for a recognized possessive subject/property occurrence, an affected ordinary-word band candidate without independent subject contact remains partial before material assembly. Rejected or empty rival material does not resurrect it, and another independently named anchor does not restore it. Retain existing name/casing protections and independently admitted context; apply the shared scoped-admission rule to body-contact units and prose pointers.
- The `rare_term` + `vector_band` pair is unchanged. Only yieldable turns pay for the early carry (about 16 ms at p95 on the embeddings job).

## Capabilities

### Modified Capabilities

- `context-activation`: a band-decided resolution yields to a retrieval carry that names a different page.

## Impact

- Code: `src/exomem/working_set.py` (`compile_packet`), `src/exomem/working_set_resolve.py` (`band_yieldable_paths`).
- Tests: `tests/test_context_activation_multilingual_embeddings.py` replaces the T6 known limit with a test of the rule and runs the English-set invariant over all 18 fixtures; `tests/test_working_set_resolve.py` covers the helper; `tests/test_working_set_carry.py` covers the yield, the name gate and the unbuildable-packet case on the core tier; the embeddings file adds an incidental note that the carry names beside M1-de, M1-ru and M5-ja.
- Behaviour: ordinary unscoped band yield remains unchanged. Recognized scoped requests can improve both semantic-evidence arms by omitting unrelated body-contact material; the compiler does not claim general property understanding. The unchanged corpus and continuity tests remain acceptance evidence, not promised unchanged scores.
