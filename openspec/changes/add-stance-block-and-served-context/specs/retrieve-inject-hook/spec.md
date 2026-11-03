## ADDED Requirements

### Requirement: Working-set injection renders the stance block and the next move

In working-set mode, when a maps packet carries a `stance` block with state `ready`, the retrieve hook SHALL render it under the fixed data header: the stance lines as whole lines with their refs, a line past its review date marked `review due`, then the `omitted` count when it is not zero. The hook SHALL render the block once per stance `generation` in a session, and on a later turn with the same generation it SHALL render one pointer line instead. A changed generation SHALL render the block again.

When a packet carries `next_action`, the hook SHALL render its `say` as one line at a fixed position, after the data block and before the ordinary reminder. The hook SHALL hold no per-state instruction line of its own: the `anchor` line that it rendered for an `ambiguous` packet, or for an `unresolved` packet with worded candidates, SHALL come only from `next_action`.

Every rendered part SHALL stay within the render ceiling, whole items only, and SHALL follow the existing failure fallback to the ordinary reminder.

#### Scenario: A changed stance generation is shown again

- **WHEN** two prompts in one session receive the same stance `generation` and a third receives a new one after the owner adds a stance line
- **THEN** the first prompt renders the stance block, the second renders one pointer line, and the third renders the block again with the new line

#### Scenario: The server's move replaces the hook's own line

- **WHEN** the service answers with an `ambiguous` abstention that carries `next_action`
- **THEN** the hook renders the competing anchors and the `say` line once, and does not add its own `anchor` instruction line

#### Scenario: A packet without a move renders no instruction line

- **WHEN** the service answers with a resolved packet that carries no `next_action`
- **THEN** the hook renders the data block and no instruction line of its own
