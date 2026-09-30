## Why

With semantic evidence on, a turn that brushes a page's rare name resolves that page on `rare_term` + `vector_band`, even when the turn is about something else. The English activation fixture T6 ("convert the grill's target temperature from Fahrenheit to Celsius") resolved the grill equipment page this way, a poison for a case that expects `unresolved`. With semantic evidence off the same turn resolves nothing, and the retrieval carry serves the page the turn actually names, an oven-temperature-conversions note. So turning the band on made T6 strictly worse than leaving it off. Tightening `rare_term` + `vector_band` for everyone is not an option: that pair is what resolves the multilingual named golds M1-de, M1-ru and M5-ja.

## What Changes

- When the band is what resolved a turn (every resolved anchor carries `vector_band` and would be `partial` without it), the compiler asks the retrieval carry what the turn would have got without the band.
- If the carry names one dominant page that is not the band's page, the band-resolved anchors are held at `partial`, the turn is `unresolved`, and it is carried exactly as it is with semantic evidence off.
- If the carry names the same page, several pages or none, the band's resolution stands. The `rare_term` + `vector_band` pair is unchanged.
- The carry is asked early only on band-decided turns, and its answer is reused by the ordinary carry, so no other turn pays for it.

## Capabilities

### Modified Capabilities

- `context-activation`: a band-decided resolution yields to a retrieval carry that names a different page.

## Impact

- Code: `src/exomem/working_set.py` (`compile_packet`), `src/exomem/working_set_resolve.py` (`band_decided_paths`, `band_yielded`).
- Tests: `tests/test_context_activation_multilingual_embeddings.py` replaces the T6 known limit with a test of the rule and runs the English-set invariant over all 18 fixtures; `tests/test_working_set_resolve.py` covers the two helpers.
- Behaviour: only turns the band alone resolved can change, and only toward the packet the off arm already serves. Nothing changes with semantic evidence off, so the v4 corpus (9/18) and continuity (4/4) are unchanged.
