---
exomem_id: 53e08a4b-0fd4-5101-9589-41c823a80f11
type: entity
entity_type: concept
status: active
created: 2026-04-14
updated: 2026-04-14
domain: hardware
tags: [hardware, storage]
---

# Network Storage

## Summary

Disk storage reachable over the home network, usually a small dedicated box that shares folders, takes backups and serves media.

## Why in the KB

The NAS build notes depend on it, and several backup habits assume it exists.

## Key ideas

- Mirroring protects against one drive failing, not against deletion or fire.
- Snapshots protect against deletion.
- An offsite copy protects against everything else.

## Relations

- relates_to [[Notes/Research/hardware/home-nas-build]]

## Home setup

The home box is a two-drive mirror with weekly snapshots and a nightly copy of the photo folder to the offsite account.
