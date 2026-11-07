## Context

The disposition that governs unasked capture has three inputs: the saved prominence preference (per principal and vault), the delegation-envelope overrides in the per-machine config file, and the gate that combines them. Bootstrap serves the result. The capture doctrine itself is product text in `prominence.CONTRACTS`, shared word for word by `balanced` and `maximal`. It is not vault data, because a vault must not redefine capture law.

## Decision

Deliver the disposition in the activation packet, computed by the server per request.

- **Why the packet.** It is the one per-turn channel that the server computes from the same gate as bootstrap. One code path reaches hooked and hookless clients, and the block arrives before the answer.
- **Why not the Stop check.** It fires after the answer, only on landings below `maximal`, and reads the level from the client machine, not the saved preference. The canonical Stop-hook requirement also forbids it to carry capture doctrine.
- **Why not the MCP server instructions.** There is one string per process while the preference is per principal and vault. The string has no room and its bound SHALL NOT rise.

## Shape

- `prominence` owns the doctrine constants and `capture_policy_text(disposition)`. Both `CONTRACTS` levels are rebuilt from the same constants, so the bootstrap bytes are identical.
- `envelope.PROACTIVE_CAPTURE_MEANING` holds one plain sentence for each value of the closed `proactive_capture` range.
- `capture_carrier` computes the block and its digest, decides whether the block is owed, and stamps the digest into the continuity token that the response returns. It never reads the turn.
- The caller echoes the token on every call, on any transport, so the token carries the delivery state. The shipped HTTP transport is stateless and gives each request a fresh session, so a per-session ledger would send the block on every call. An old token has no digest field, so its holder is owed the block.
- `op_activate_context` reserves the block's characters in the map budget when the block is owed and the map keeps at least the smallest budget a request may ask for, and attaches it outside the packet cache. A smaller request leaves the block owed, and the token is not stamped.
- A packet that carries no token, such as the abstention under `EXOMEM_DISABLE_WORKING_SET`, has nothing to stamp and so carries the block on every request.
- The hook prints the block as served. The server owns de-duplication, so the hook keeps no state file.

## Risks

- A token near its length limit cannot take the digest. It is returned unchanged and its holder is owed the block again.
- The line may lower precision, so quiet cases start writing. The proof plan includes negative cases.
- The shared adoption sentence may still suppress a save under `silent`. If a trial shows it, reword the shared constant once, for both carriers.
- If a trial shows agents still ask permission with the block in context, the limit is model caution, not delivery.
