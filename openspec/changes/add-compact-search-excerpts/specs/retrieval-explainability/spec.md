## MODIFIED Requirements

### Requirement: Retrieval Explanation Is Explicit And Backward Compatible
`find` and `ask_memory` SHALL accept `explain: bool`, default `false`. When omitted or false, existing compact and full response bytes, fields, and ordering MUST remain compatible. When true, recall SHALL add a bounded top-level `retrieval_profile` and per-hit `ranking_explanation` without adding note bodies, excerpts, unit content, or other full-detail fields solely because explanation was requested.

#### Scenario: Ordinary compact recall is unchanged
- **WHEN** the same compact request is executed before and after this capability with `explain` omitted
- **THEN** its serialized response and hit ordering are unchanged

#### Scenario: Compact explanation does not leak content
- **WHEN** compact recall uses `explain=true`
- **THEN** diagnostic ranking metadata is added but note bodies remain absent
- **AND** every compact page-hit `excerpt` is the same bounded excerpt that the request carries without `explain`; requesting an explanation adds none
