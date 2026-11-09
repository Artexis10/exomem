## MODIFIED Requirements

### Requirement: Incomplete Exact Recall Is Observable

Internal index outcomes SHALL distinguish `available`, `stale`, `unsupported`, `transient_failure`, and `fatal_failure` and carry a completeness flag. If a safe exact category/kind request lacks a complete catalog and cannot apply a complete bounded delta, `find` SHALL return a typed `RETRIEVAL_INDEX_WARMING` operation error with `complete=false`, `status` equal to `warming` or `temporarily_unavailable`, and bounded `retry_after_ms`. It MUST NOT return or cache an authoritative empty hit list. The MCP result SHALL carry the same code in its `error` object, with a `retry` whose `after_s` is the bounded wait in whole seconds; the other fields stay in the REST and CLI error block.

#### Scenario: Missing catalog is warming, not no results

- **WHEN** a safe exact category request arrives before catalog readiness
- **THEN** every command surface reports `RETRIEVAL_INDEX_WARMING`, and REST and CLI carry `complete=false`
- **AND** the MCP result carries the code and `retry.after_s`
- **AND** retry metadata is bounded and the response is excluded from the hot result cache
