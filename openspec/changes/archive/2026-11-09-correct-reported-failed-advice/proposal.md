## Why

An agent recommended a command that the user had already found does not work. The capture guidance did not name the user's report that stored advice failed as durable, so no agent corrected the note, and recall kept returning the advice as authoritative. The recall guidance also let the agent treat advice about the user's own system as a general question, so it answered without recall.

## What Changes

- The `balanced` and `maximal` capture contracts and the activation `capture_policy` block serve one sentence: stored advice the user says failed, or a claim the user corrects, is durable and is corrected under `proactive_capture`.
- The Stop-hook capture check names the same case.
- The server instructions and the native MCP activation nudge count advice on the user's own case as clear relevance.
- The rendered `capture_policy` block limit rises from 1,200 to 1,320 bytes to carry the sentence. The bootstrap core and the 900-character server-instruction bound stay unchanged; `maximal` pays its bytes by dropping two clauses that repeat other sentences.

## Capabilities

### Modified Capabilities

- `agent-prominence-control`: adds the correction rule and the advice-turn relevance.
- `context-activation`: the capture-policy block carries the correction rule; its limit becomes 1,320 bytes.
- `agent-bootstrap-contract`: the capture check names the correction case.
