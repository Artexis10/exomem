# Proposal

## Why

Captured documents rarely reach the agent when the owner asks about them. Context activation (`recall_context`) drops every page under `Sources/` and `Evidence/` before it ranks anything. A captured document is therefore invisible to the default recall path until someone compiles a note from it.

Most captured material is never compiled. Some folders, such as bulk exports and recordings, were never compile candidates. A document captured today stays invisible however plainly a later question names its content. Ordinary search already finds these pages, so the gap is specific to activation.

## What Changes

- Activation may carry a captured Source or Evidence page when it is the best or only evidence that the turn names.
- Compiled notes keep precedence. A compiled note wins over a captured page of similar strength. A captured page that a carried compiled note cites is attached to that note, not carried a second time.
- Each carried captured page is labelled as uncompiled source material, so the agent reads it as attributed material and not as a conclusion.
- One configuration value bounds the captured pages per packet. A bound of 0 restores the previous packet exactly.
- Captured pages never become anchors, and the `anchor` override still refuses them.
- Existing governance and egress rules apply to captured pages unchanged.
- Ordinary search applies the media exemption from the compiled preference in its rerank path as well, through the same rule as its other paths.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: the packet may carry bounded, labelled captured material; anchors still exclude it.

## Impact

The change affects the activation carry lanes in `working_set_runtime`, the packet assembly in `working_set`, the ranking configuration and the private real-turn acceptance script. It adds no model, worker, queue or background job, and it rewrites no stored data. Undo is the configuration bound set to 0, with no deploy.
