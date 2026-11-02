## Why

A scene-frame page holds text taken from its video, such as a description of the frame. Scope membership evaluated the frame page as its own item. A scope that selects the video by its stable identifier or by project therefore did not cover its frames, and a frame's text could be released where the video is withheld. Extracted-text part pages already take their Source page's membership; frames need the same rule.

## What Changes

- A scene-frame page takes the scope membership, and with it the audiences, of its video's Source page.
- The redirect needs the frame's path shape and its `parent_media` field together. A page that only claims a parent in front matter is governed as itself.

## Impact

- Code: `src/exomem/governance/membership.py`.
- Specs: `governance-kernel`.
