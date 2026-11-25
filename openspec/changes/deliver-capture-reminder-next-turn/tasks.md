## 1. Delivery setting and deferral

- [x] 1.1 Resolve `EXOMEM_CAPTURE_NUDGE_DELIVERY`, then `capture_nudge_delivery`, then `stop`; treat an unknown value as `stop`.
- [x] 1.2 In `next-turn` mode, write the session's pending marker in place of the block, keep the cooldown, log `nudge deferred`, and block as before when the marker cannot be written.
- [x] 1.3 Keep the episode ask and the coverage ask blocking in both modes.

## 2. Delivery on the next prompt

- [x] 2.1 Add the reminder to the next prompt's `additionalContext`, once, in both the legacy and the native-activation paths of the prompt hook, from the wording in the capture hook.
- [x] 2.2 Drop a marker older than six hours, and never fail the prompt on a state error.
- [x] 2.3 Log `nudge delivered` in the capture-nudge log.

## 3. Proof

- [x] 3.1 Run the real hook scripts as subprocesses: deferral, one delivery on a prompt the retrieval hook would skip, the shared reply, the stale marker, the episode ask, an unknown value, a failed deferral and Codex.
- [x] 3.2 Regenerate the committed hook copies.

## 4. Trial

- [ ] 4.1 Run `next-turn` for one week and count Exomem saves per reminder in each mode from the capture-nudge log.
- [ ] 4.2 Decide the default from that count: keep `stop`, or switch to `next-turn`. Revert if saves drop.
