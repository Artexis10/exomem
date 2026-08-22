## Why

A structural-promotion suggestion that the user has already acted on never stops firing.

`add-structural-promotion-suggestions` shipped with no resolution mechanism on the stated assumption that the advisory "resolves by scope agreement" — recorded as the out-of-scope line in `close-write-warning-suppression`. That assumption holds only for restructure-by-*subtraction*: the shipped control `test_advice_stops_once_the_material_is_routed_into_matching_scope` asserts the origin page is quiet again after "the material removed", and task 1.9 of the original change describes it as "the cleaned original".

Real restructures are additive. Durable units carry anchors, relation targets and history, so an agent that acts on the advice creates the destination pages and leaves the origin units in place. The origin page keeps every off-scope unit it had, so every gate still holds and the advisory re-fires on every subsequent write, forever, about material that already has a home.

Replaying the merged detector against a real dogfooded page confirms it. A travel page that accumulated a property/livestock/financing cluster was split into six destination notes on 2026-08-15; the origin page still returns `strength: strong`, `off_scope_units: 16`, all four reason codes, `cluster_terms: agriculture, cattle, champagne, financing, mortgage, property` — every one of those terms declared by a destination that exists *because the user acted*. Sixteen of the cluster's twenty-one recurring terms are, and the advisory reports them anyway, on every write, indefinitely.

The cost is not cosmetic. A channel that cannot be resolved teaches its reader to stop reading it, which mutes the true positives too — the habituation failure the 2026-08-15 no-nudge audit rated the cheapest defect on the delivery path. Pre-registered family `f25 restructure_lifecycle` already measures exactly this and is expected-red: "applying a suggestion resolves it: the signal disappears because the state changed, not because anyone dismissed it."

## What Changes

- Treat a cluster as **routed** when the vault already contains eligible compiled destinations whose declared identity covers its recurring vocabulary, and re-evaluate the suggestion with routed terms removed. A cluster that no longer clears the mass gate produces no suggestion.
- Resolve by corpus state only. Record no dismissal, no acceptance, no cooldown, and no per-page suggestion history, so a destination that is later deleted or renamed brings the advisory back.
- Require each contributing destination to cover at least two cluster terms, so an unrelated page carrying one incidental tag can never scavenge a cluster into silence.
- Read destinations only from the corpus context the write already built, and retain that context on the creation preflight, which currently discards it — no new index, table, query, walk, embedding, or model call, and no second corpus build.
- Keep the emitted payload byte-identical: no new key, no new reason code, no numeric confidence. Resolution is expressed only as the absence of a suggestion.
- Keep the failure isolation: an unavailable or unreadable corpus leaves the detector behaving exactly as it does today, emitting the suggestion rather than suppressing it.

## Capabilities

### Modified Capabilities

- `command-surface`: a structural suggestion resolves when the corpus contains eligible destinations declaring the cluster's vocabulary, without recording any dismissal state, and the detector degrades to current behaviour when no corpus is available.

## Impact

The detector module and its tests, the creation preflight (one retained field), and the two commit functions that pass the corpus through. No MCP tool, tool description, parameter, or input schema changes, so the packaged tool-surface fingerprint and the external connector attestation are untouched. No new store, index, background worker, or synchronous embedding. Existing write-latency, governance, and mutation-safety gates continue to apply unchanged, and the detector remains pure over state the caller already holds.

Deliberately out of scope: dismissal, snooze or cooldown state of any kind (`f25` fails a product that clears its own suggestion by recording a dismissal); detection threshold changes; naming the destination in the payload, which stays the agent's job; any new structural signal family such as commitment or granularity detection; whole-corpus structural audits; and any migration preview or execution.
