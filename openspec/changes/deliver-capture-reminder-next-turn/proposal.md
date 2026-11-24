# Proposal

## Why

The Stop hook sends the per-turn capture reminder by blocking the stop. A block forces one extra model turn right after the turn that landed work. A replay of 14 days of one installation counted 719 forced turns, and 89 of them (12%) led to an Exomem save. Most forced turns cost a full-context model call and save nothing.

The same reminder can reach the agent as context on the session's next prompt. That turn happens anyway, so the reminder costs no extra turn. Whether the agent saves as often when it reads the reminder there is not known. The change makes the delivery mode a setting, so the two modes can be compared.

## Decision

The decision is the reminder's delivery mode: `stop` (block the stop, today's behaviour) or `next-turn` (leave the reminder for the next prompt). A durable spec helps because two hooks, in two scripts and two clients, must agree on the setting, the state file and the wording, and a later change of the default needs a stated contract to revise.

`stop` stays the default. `next-turn` is opt-in for a one-week trial. After the week, the saves per reminder in each mode decide the default, and a drop in saves reverts it.

## What Changes

- **Delivery setting.** `EXOMEM_CAPTURE_NUDGE_DELIVERY`, then the config key `capture_nudge_delivery`, then `stop`. The values are `stop` and `next-turn`; any other value means `stop`.
- **Deferral.** In `next-turn` mode the Stop hook applies every existing gate, then writes a pending marker for the session beside its cooldown stamp and does not block. It records the cooldown and logs `nudge deferred`. If it cannot write the marker, it blocks as in `stop` mode.
- **Delivery.** The UserPromptSubmit hook adds the same reminder text to the session's next prompt, once, even on a prompt where it otherwise stays silent, and logs `nudge delivered`. A marker older than six hours is dropped. The reminder wording stays in the capture hook.
- **Unchanged.** The episode ask and the coverage ask block in both modes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-bootstrap-contract`: the client capture reminder can be delivered on the session's next prompt.

## Impact

The changed code is in `_hooks/exomem_capture_nudge.py` and `_hooks/exomem_retrieve_nudge.py`. The committed copies under `plugins/` are regenerated. Both Claude and Codex run the two hooks and resolve one state directory for each.
