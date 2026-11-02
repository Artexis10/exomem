## 1. Hook

- [x] 1.1 Evict the least recently changed session when a new session finds the catalog full (`test_a_full_catalog_evicts_the_oldest_session_for_a_new_one`).
- [x] 1.2 Drop catalog entries whose root entry is gone before evicting (`test_catalog_entries_without_a_directory_are_dropped_to_make_room`).
- [x] 1.3 Refuse a new session without a partial write when nothing can be evicted (`test_catalog_capacity_exhaustion_soft_fails_without_unregistered_publication`).
- [x] 1.4 Prune before the checkpoint write (`test_prune_runs_when_the_checkpoint_write_fails`).
