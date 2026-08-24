## Why

The checked marketplace reviewer fixture can pass static digest checks while containing notes that the governed `remember` path refuses, so the first real provider run discovers an invalid release artifact after a one-shot reviewer window has started. The bootstrap driver also issues reviewer credentials before the required fixture is seeded, sealing the only ordinary setup access and forcing a human to reconstruct a deterministic operator workflow in ChatGPT or Claude.

## What Changes

- Make every pre-seeded reviewer note valid under the current semantic-authoring and relation-review contracts without weakening either contract.
- Add executable fixture conformance that creates the exact checked payload through the real `remember` leaf, including the validate-to-commit reviewed-none handshake when a note has no honest qualifying relation.
- Change the reviewer bootstrap sequence to wait for `CELL_READY`, seed and verify the exact fixture through authenticated MCP, and only then issue the Claude and OpenAI reviewer credentials that seal setup access.
- Fail closed without reporting bootstrap completion or sharing credentials when readiness, fixture mutation, fixture verification, or fixture identity binding fails.
- Keep native ChatGPT and Claude acceptance manual: automation prepares deterministic state but does not manufacture provider evidence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hosted-marketplace-release`: Require the checked reviewer fixture to be executable through the governed product write path and require the reviewer bootstrap to seed and verify it before credential issuance seals setup access.

## Impact

- The v1 reviewer fixture, its v2 successor, and deterministic marketplace artifacts bound to the fixture version and payload digest.
- `scripts/reviewer_bootstrap.py` ordering, readiness polling, authenticated MCP transport, secret handling, and failure receipts.
- Marketplace fixture and reviewer bootstrap tests, including a real local-vault write-path test and call-order/fail-closed CLI tests.
- The next Hosted candidate and paired provider promotion window; the current candidate/window cannot provide honest evidence after its fixture digest changes.

No optional model or heavy capability is introduced. The seeder is deterministic transport and contract execution only, consistent with Exomem's pure-substrate boundary.
