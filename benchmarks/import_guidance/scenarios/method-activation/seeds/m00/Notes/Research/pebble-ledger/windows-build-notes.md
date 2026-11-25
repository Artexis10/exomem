---
exomem_id: 2a37f8ba-e0f2-59ac-a210-15a0c25b9521
type: research-note
project: pebble-ledger
status: active
created: 2026-05-17
updated: 2026-10-02
sources: []
tags: [software, windows, ci]
---

# Windows build notes

## Question

What does it take to keep the Windows binary of Pebble Ledger building next to the Linux and macOS ones?

## Findings

- The release job builds three binaries on three hosted runners. The Windows one is the slowest and the most fragile.
- Path handling bit twice: a hard-coded forward slash in the config lookup, and a temp-file rename that fails while another process holds the file.
- Antivirus software on the Windows runner occasionally locks freshly built executables, which shows up as a flaky test rather than a build error.
- The Windows build is the first thing to check whenever a new build step is added to the pipeline.

## Open threads

- Whether a local Windows virtual machine is worth keeping for quick checks.

## Relations

- about_entity [[Entities/Concepts/Pebble Ledger]]
- relates_to [[Notes/Procedures/pebble-ledger-release-checklist]]
