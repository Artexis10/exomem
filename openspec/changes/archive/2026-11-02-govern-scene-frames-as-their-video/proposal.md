## Why

A scene-frame page holds text taken from its video, such as a description of the frame. Scope membership evaluated the frame page as its own item. A scope that selects the video by its stable identifier or by project therefore did not cover its frames, and a frame's text could be released where the video is withheld. Extracted-text part pages already take their Source page's membership; frames need the same rule.

## What Changes

- A scene-frame page belongs to its own scopes and to its video's. A decision takes the most restrictive of an item's scopes, so the added scopes can only narrow who reads a frame, and a page that claims a parent gains nothing.
- A page takes its video's scopes only when its path shape and its `parent_media` field agree.
- A projected catalog row of a frame holds the video's scopes as of the frame's publication. A later change to the video's membership reaches the frame's row only when the frame is published again. A row published before this change keeps only the frame's own scopes until the frame is published again or the owner refreshes the projection.

## Impact

- Code: `src/exomem/governance/membership.py`, `src/exomem/governance/lifecycle.py` (recovery treats an unresolved item as governed, as deletion does).
- Specs: `governance-kernel`.
