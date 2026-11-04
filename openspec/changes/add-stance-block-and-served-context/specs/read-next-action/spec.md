## Purpose

Give clients one capability-aware next move from a shared source while preserving existing recovery and packet budget semantics.

## ADDED Requirements

### Requirement: Read responses carry one next move

Activation and search SHALL carry one `next_action` in their structured result when a state applies, and omit it otherwise. Its shape SHALL be `{state, say, tool?, args?}`. `say` SHALL contain at most 240 characters. Include `tool` only for a one-tool move and `args` only when complete. Render named calls through the existing capability renderer; never advertise an unavailable call.

Add the move after cache lookup, never inside the cached packet. Reserve 240 characters of counted prose before packet construction and charge new move prose under the existing budget definition. This SHALL NOT impose a serialized-JSON cap: refs, tools, arguments and structure keep their existing treatment. Refresh `serialized_chars` after final additions. Failure SHALL omit the move, report `next_action_unavailable` in diagnostics and leave the read served.

#### Scenario: No state means no move

- **WHEN** no central state applies
- **THEN** the response contains no `next_action`

#### Scenario: Final size reports the final response

- **WHEN** a response receives a move and record id after cache lookup
- **THEN** counted prose obeys its existing cap and `serialized_chars` reports the final serialized packet

### Requirement: One source preserves every recovery move that it replaces

One central source SHALL select the first applicable activation move:

1. `ambiguous`: existing anchors ambiguity or unresolved worded candidates; ask for the supported anchor selection.
2. `review_due`: an admitted served stance line needs review; apply it with the review caveat.
3. `slice_empty`: an empty maps slice; explain compiled coverage and point to the exposed search route.
4. `related`: the existing anchors related-pages condition without a stronger move; explain that related pages are not loaded and direct the agent to read a relevant one.

For search, `search_miss` SHALL carry the current miss rule when no result exists. Its removal from bootstrap SHALL preserve the coordinated `shrink-bootstrap` and reduced-surface contracts. States are closed protocol values. Selection SHALL use existing packet states, not client names or semantic cue lists. Anchor moves SHALL apply only where `anchor` has its existing effect; maps slice ambiguity SHALL NOT imply that an anchor pick changes maps mode.

#### Scenario: Related pages keep their recovery move

- **WHEN** an anchors packet is unresolved, carries related pages and has no worded candidate or stronger move
- **THEN** the central move says that the pages are related and not loaded, and directs a relevant read

#### Scenario: Review wins over an empty slice

- **WHEN** a served stance line is due for review and the maps slice is empty
- **THEN** exactly one move carries `state: "review_due"`

#### Scenario: A maps lead does not gain anchor semantics

- **WHEN** a maps slice contains partial or ambiguous leads
- **THEN** the table does not promise that selecting `anchor` changes that slice

### Requirement: Static carriers share current and compatibility text

Server instructions, read tool descriptions and scaffold guidance SHALL point to `next_action` when present. Keep server instructions within 900 characters. Preserve capability-aware bootstrap carrier selection, including a search carrier on surfaces without activation.

The standalone hook's bounded older-service fallback SHALL be generated from the same move source through the existing artifact pipeline. It SHALL preserve the recovery instructions that a supported older service lacks and SHALL never duplicate a supplied move. No independent hand-authored per-state copy SHALL remain.

#### Scenario: A reduced surface retains recall guidance

- **WHEN** the exposed surface offers search but not activation
- **THEN** bootstrap retains its supported search carrier and adds the next-action guidance without naming an unavailable call

#### Scenario: An older service retains anchor recovery

- **WHEN** a supported older service returns an ambiguous anchors packet without `next_action`
- **THEN** the hook renders the shared generated anchor-recovery instruction
