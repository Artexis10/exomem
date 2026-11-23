---
exomem_id: 547dd0bc-4275-54c3-9f4f-2f6e05a0d949
type: failure
status: active
created: 2026-05-12
updated: 2026-05-12
sources: []
projects: [hardware]
tags: [hardware, network]
---

# Router update dropped the wifi

## What happened

After an automatic firmware update the router came back with the wireless network switched off and the admin password reset.

## Cause

The update restored factory defaults when its settings format changed, and no backup of the configuration existed.

## Lesson

Export the router configuration before any firmware update, turn off automatic updates, and keep the admin password in the password manager.

## Follow-up

The configuration export now lives in the hardware folder next to the NAS notes, and the router is on the manual update list.
