# Require a Hash for Every Whole-Body Edit

## Why

A `read_memory` window of a long Source returns no `content_hash`, because a window is not the whole page. `edit_memory` with `replace_body` treated `expected_hash` as optional. A caller could therefore build a new body from part of a page and overwrite the whole page. The owner chose to close that path: a whole-body write must name the exact page it replaces.

## What Changes

- The `replace_body` variant of `edit_memory` advertises `expected_hash` as a required field.
- Every surface refuses a whole-body edit without `expected_hash` before mutation: `edit_memory` on MCP, REST and CLI, nested or flat, and the opt-in legacy `edit` alias. A null hash is refused the same way, and so is a preview (`validate_only`).
- The refusal names the field and says where the hash comes from: the `content_hash` of a whole-page read.
- Other edit kinds keep `expected_hash` optional.

## Impact

- Clients that send `replace_body` without a hash now get `INVALID_EDIT` and must read the page first.
- The packaged MCP tool-surface digest changes, so the ChatGPT Personal Plugin needs its ordinary post-deploy refresh.
