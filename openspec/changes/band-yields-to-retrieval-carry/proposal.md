## Why

With semantic evidence on, a turn that brushes a page's rare name resolves that page on `rare_term` + `vector_band`, even when the turn is about something else. The English activation fixture T6 ("convert the grill's target temperature from Fahrenheit to Celsius") resolved the grill equipment page this way, a poison for a case that expects `unresolved`. With semantic evidence off the same turn resolves nothing, and the retrieval carry serves the page the turn actually names, an oven-temperature-conversions note. So turning the band on made T6 strictly worse than leaving it off. Tightening `rare_term` + `vector_band` for everyone is not an option: that pair is what resolves the multilingual named golds M1-de, M1-ru and M5-ja.

## What Changes

- A band resolution is yieldable when the band is what resolved the turn (every resolved anchor carries `vector_band` and would be `partial` without it) and the anchor's rare word was written as an ordinary word: in lower case, in a turn whose casing marks names elsewhere. This is the existing `name_lower_case` signal that bare-name ambiguity already reads.
- On a yieldable turn the compiler asks the retrieval carry what the turn names. If the carry names one dominant page that is not the band's page, and a carried packet builds from it, that packet is served, the same one the turn gets with semantic evidence off.
- In every other case the band's resolution stands: a capitalised name or a name in a script with no case, the carry naming the same page, several pages or none, or a carried page the lanes read nothing off. A yield never leaves the turn with nothing, and a phrase an unrelated note shares with the turn never overrules a rare name.
- The `rare_term` + `vector_band` pair is unchanged. Only yieldable turns pay for the early carry (about 16 ms at p95 on the embeddings job).

## Capabilities

### Modified Capabilities

- `context-activation`: a band-decided resolution yields to a retrieval carry that names a different page.

## Impact

- Code: `src/exomem/working_set.py` (`compile_packet`), `src/exomem/working_set_resolve.py` (`band_yieldable_paths`).
- Tests: `tests/test_context_activation_multilingual_embeddings.py` replaces the T6 known limit with a test of the rule and runs the English-set invariant over all 18 fixtures; `tests/test_working_set_resolve.py` covers the helper; `tests/test_working_set_carry.py` covers the yield, the name gate and the unbuildable-packet case on the core tier; the embeddings file adds an incidental note that the carry names beside M1-de, M1-ru and M5-ja.
- Behaviour: only turns the band alone resolved on an ordinary word can change, and only toward the packet the off arm already serves. Nothing changes with semantic evidence off, so the v4 corpus (9/18) and continuity (4/4) are unchanged.
