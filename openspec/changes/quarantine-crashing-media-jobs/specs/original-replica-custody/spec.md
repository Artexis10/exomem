## ADDED Requirements

### Requirement: Offload with no allowance probes no reuse candidate

When the media store policy sets no allowance, or one that is not a byte count, offload SHALL refuse a filing that it would copy as `MEDIA_ALLOWANCE_UNKNOWN`. It SHALL refuse before it reads the census or asks the store about a reuse candidate. This is the one exception to the rule that offload records a lost candidate of a filing with no bytes on the volume. While no allowance is set, such a loss therefore stays unrecorded.

#### Scenario: An offload under a policy with no allowance

- **WHEN** offload would copy a filing under a store policy that sets no allowance
- **THEN** the offload is refused as `MEDIA_ALLOWANCE_UNKNOWN`
- **AND** it reads no census and asks the store about no reuse candidate
