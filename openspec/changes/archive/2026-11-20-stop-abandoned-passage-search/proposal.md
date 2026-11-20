## Why

A passage request keeps running after its caller times out. The serving host records the departure, but the worker thread that runs the search is never told. The abandoned request then finishes unseen and keeps its model admission slot until it does. A client that times out at one second and retries fills the four slots with work nobody reads, and new requests are refused as busy.

## What Changes

- A passage request checks whether its caller has left at each boundary between its phases: eligibility, query encoding, chunk selection and byte verification.
- When the caller has left, the request stops at that boundary and releases its admission slot. The cell logs `passage_search_abandoned` with the phase it stopped before.
- Busy semantics do not change. A request that cannot get an admission slot at once is still refused with a busy outcome and a retry hint.
- Other tools do not change. A capture whose caller leaves still completes, because its caller adopts the settled write later.

## Capabilities

### Modified Capabilities

- `passage-evidence`: an abandoned passage request stops at its next phase boundary and frees its admission.

## Impact

- `src/exomem/passage_evidence.py`: the request checks the serving host's cancel scope between phases.
- A command-line or in-process request has no serving host, so it always runs to completion.
