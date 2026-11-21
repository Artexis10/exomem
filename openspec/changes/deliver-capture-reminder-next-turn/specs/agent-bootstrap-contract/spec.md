## ADDED Requirements

### Requirement: The client capture reminder can be delivered on the session's next prompt

Where a client hook sends the per-turn capture reminder, a delivery setting SHALL choose when the reminder reaches the agent. The value `stop` SHALL block the stop with the reminder. The value `next-turn` SHALL leave the reminder for the session's next prompt and SHALL NOT block the stop.
The setting SHALL resolve from the environment variable `EXOMEM_CAPTURE_NUDGE_DELIVERY`, then from the key `capture_nudge_delivery` of the machine config file, then default to `stop`. The first layer that sets a value decides, and any value other than `next-turn` SHALL mean `stop`.
In `next-turn` mode the Stop hook SHALL apply every gate of the per-turn reminder, including the landing or length gate, the knowledge-base write gate, `stop_hook_active` and the cooldown. It SHALL then record the cooldown, log `nudge deferred` and leave a pending marker for the session beside its cooldown stamp. If it cannot write the marker, it SHALL block the stop as in `stop` mode.
On the session's next prompt the UserPromptSubmit hook SHALL add the same reminder text to its `additionalContext`, whatever its own gates decided for that prompt, then delete the marker and log `nudge delivered`, so the reminder reaches the agent once. It SHALL drop a marker older than six hours without delivering it. A failure to read or delete the marker SHALL NOT fail the prompt hook.
The episode ask and the candidate-coverage ask SHALL block the stop in both modes. The Stop hook and the UserPromptSubmit hook SHALL resolve the same state directory for each client.

#### Scenario: A deferred reminder arrives on the next prompt

- **WHEN** `next-turn` is set and a turn at `balanced` runs `git push` successfully and wrote nothing to the knowledge base
- **THEN** the stop is not blocked and the session's pending marker exists
- **AND** the session's next prompt carries the capture reminder in its `additionalContext`, even when the prompt is too short for the retrieval hook to answer

#### Scenario: A deferred reminder is delivered once

- **WHEN** the prompt after a deferred reminder has carried it
- **THEN** the pending marker is gone and the following prompt carries no capture reminder

#### Scenario: A stale reminder is dropped

- **WHEN** a pending marker is more than six hours old at the session's next prompt
- **THEN** the prompt carries no capture reminder and the marker is deleted

#### Scenario: The episode ask still blocks

- **WHEN** `next-turn` is set and the episode ask is due on a Stop
- **THEN** the Stop is blocked with the episode ask and no pending marker is written

#### Scenario: The default stays stop

- **WHEN** no delivery setting is present, or its value is not `next-turn`
- **THEN** a landing turn blocks the stop with the capture reminder as before

#### Scenario: A reminder that cannot be deferred still arrives

- **WHEN** `next-turn` is set and the pending marker cannot be written
- **THEN** the Stop is blocked with the capture reminder
