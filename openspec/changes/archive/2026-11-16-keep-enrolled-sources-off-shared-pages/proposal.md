## Why

A service caller's capture under erasure custody added a line for the Source to the top index, the activity log and the Sources index.
Erasure then needed per-line ownership on those shared pages, so every later write to them carried generated custody.
In a vault with enrolled Sources, an unenrolled `save_source` or `create_note` made nine commits instead of one and took several seconds.
The captured-Source inventory already lists enrolled Sources for the service caller, so the shared-page lines add no access that the caller lacks.

## What Changes

- A Source captured under erasure custody adds no line to the top index, the activity log or the Sources index.
- The captured-Source inventory stays the listing for those Sources.
- Sources outside custody, such as Sources that an agent saves, keep their listing on the shared pages.
- A vault with no shared-page custody writes those pages without generated custody.
- A vault where an earlier build recorded enrolled lines keeps carrying their ownership, so a later erasure can still find every copy.

## Capabilities

### Modified Capabilities

- `captured-source-inventory`: the inventory becomes the only listing of enrolled Sources; the shared generated pages exclude them.

## Impact

The change touches Source capture planning, the shared-page renderer and the generated-output producer.
Source counts on the shared pages still include enrolled Sources; a count carries no Source text.
The erasure design keeps its shared-page cleanup only for ownership that earlier builds recorded.
