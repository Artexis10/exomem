## 1. Keep recall caches until memory pressure

- [x] 1.1 Gate the idle release of CPU cache slots in normal mode on the host or cgroup memory reading, with the floor as a setting and release on an unknown reading.
- [x] 1.2 Cover the plentiful, short, unknown and configured-floor cases and the cgroup headroom reading with reaper tests.
- [ ] 1.3 After release on the personal service in normal mode, show that a recall after more than fifteen idle minutes no longer rebuilds the find caches (no `reaped idle model find-ram-caches` while memory is plentiful).
