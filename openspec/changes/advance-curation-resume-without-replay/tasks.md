## 1. Implementation

- [x] 1.1 Exempt a curation `resume` from implicit retry replay at the common invocation boundary. Proved by `test_an_identical_curation_resume_advances_the_run_instead_of_replaying`, which fails before the change, and `test_an_mcp_curation_resume_without_a_key_completes_its_vocabulary_application`, which fails when a resume has no identity at all. Catches: a multi-step curation run that stalls for 600 seconds per step on MCP.

## 2. Release check

- [ ] 2.1 After the personal service runs a release with this change, advance a live curation run through MCP with back-to-back `resume` calls. Expect one new committed step per call. Proved by the run's status after the calls. Catches: a deployment path that still answers a `resume` from the retry window.
