---
exomem_id: 934f8dac-d6ab-57c6-bec7-33e9db09b711
type: procedure
status: active
created: 2026-09-01
updated: 2026-10-05
sources: []
summary: Reference for parser maintenance and delivery in the Alderframe CLI repository.
tags: [repository, delivery, parser]
---

# Alderframe repository delivery

## Scope

Use as reference for parser maintenance, pull-request merge and release preparation in the Alderframe CLI repository.
This page is not owner-approved. It grants no authority to publish or change a repository.

## Method

- [technique] For an Alderframe parser change, check the empty-argument regression with `python -m pytest tests/test_arguments.py`, then run the CLI smoke check with `python -m alderframe --help`. Preserve the public argument names. ^parser-checks
- [technique] For an Alderframe pull request whose required checks passed and whose independent review accepted the current head, prepare a squash merge that deletes its branch. For pull request 42, the command is `gh pr merge 42 --squash --delete-branch`. A prepared command does not prove a merge occurred. ^merge
- [technique] For an Alderframe release, verify the merged main head and its CI before creating the next patch tag with a `v` prefix. From version 1.4.2, the next patch tag is `v1.4.3`. The release workflow publishes the tag; verify its result before claiming publication. ^release

## Not for

The Berylport service repository or an unnamed standalone helper.
