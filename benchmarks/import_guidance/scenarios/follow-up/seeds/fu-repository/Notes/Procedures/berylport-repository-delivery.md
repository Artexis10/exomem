---
exomem_id: 4a229c75-71ad-5ac7-b975-23ebf1b49e95
type: procedure
status: active
created: 2026-09-01
updated: 2026-10-05
sources: []
summary: Reference for request-handler maintenance and delivery in the Berylport service repository.
tags: [repository, delivery, service]
---

# Berylport repository delivery

## Scope

Use as reference only for the Berylport service repository.
This page is not owner-approved. It grants no authority to publish or change a repository.

## Method

- [technique] For Berylport handler changes, run `python -m pytest tests/test_handlers.py`. Preserve the request header names. ^handler-checks
- [technique] Berylport uses a merge commit and release tags with a `service-` prefix. Verify the release workflow before claiming publication. ^delivery

## Not for

The Alderframe CLI repository or an unnamed standalone helper.
