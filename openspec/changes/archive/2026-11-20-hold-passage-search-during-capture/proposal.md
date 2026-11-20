## Why

A capture encodes every declared passage of its Source before it commits. The served encoder runs that work one inference per model turn, about half a second each. A passage request encodes its query in the same serialized turn, so during a capture each request waits for about one passage inference. With one caller, p95 rises from about 0.2 s to about 1 s. With four callers, the capture and the waiting requests hold all four model admissions, and most new requests are refused as busy.

A request whose caller left also keeps waiting for its turn. It holds its admission until it gets the turn and reaches the next phase boundary, so a client that times out and retries is refused as busy.

## What Changes

- On a served encoder that runs inferences side by side, a passage request takes its execution turn on a lane for passage query encodes. It no longer waits for the passage inferences of a capture or a background drain.
- Model admission does not change. A passage request still takes one admission from the shared process budget and is refused as busy when none is free.
- A passage request that waits for its execution turn leaves the wait when its caller leaves. It releases its admission and logs `passage_search_abandoned` with phase `encode`.
- A model that cannot run inferences side by side keeps the shared serialized turn.

## Capabilities

### Modified Capabilities

- `passage-evidence`: a passage query encode no longer waits for a capture's passage inferences, and an abandoned request leaves its model-turn wait at once.

## Impact

- `src/exomem/embeddings.py`: `query_turn` gives a passage query encode its own execution lane on a concurrent encoder.
- `src/exomem/passage_evidence.py`: the query encode runs in `query_turn` and stops its wait when the caller leaves.
- `src/exomem/runtime_resources.py`: `model_execution` accepts a stop event.
