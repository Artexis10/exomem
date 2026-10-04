## ADDED Requirements

### Requirement: Stop-hook blocks are one short paragraph

Where a client Stop hook blocks to ask for a capture or an episode record, the reason SHALL be one short paragraph, asserted by a byte ceiling per block. The capture check SHALL be the same short text on every fire, with no first-fire or post-compaction full text, and SHALL NOT carry the capture doctrine. It SHALL name the rules that prevent known incidents (the live-policy pointer, no transcripts, `replace_memory` supersedes a contradicted conclusion, stated intent to Planning and observed outcome to Records, transient code, test and CI output stays out, and nothing durable means stop) and SHALL end with a pointer to the shipped engagement reference by a vault path that `read_memory` opens (`.exomem/schema/references/engagement.md`), where the full capture rules live. The episode ask SHALL keep the `episode_memory` record call, the episode key and its do-nothing escape. When the hook runs against the native MCP connection, its preamble SHALL be one clause of at most 90 bytes that says to skip when Exomem is not connected or cannot capture and to bootstrap first if the contract is absent. Shortening a block SHALL NOT change when it fires.

#### Scenario: Every capture fire is the short check

- **WHEN** the capture check fires three times in one session, or fires after a compaction
- **THEN** each fire carries the same short check

#### Scenario: A block is short in MCP mode

- **WHEN** the capture check or the episode ask fires against the native MCP connection
- **THEN** the whole reason, preamble included, is within its byte ceiling and the preamble is at most 90 bytes

#### Scenario: The pointer reaches the rules

- **WHEN** an agent with only the MCP connection follows the capture check's pointer
- **THEN** `read_memory` returns the shipped engagement reference from the vault
