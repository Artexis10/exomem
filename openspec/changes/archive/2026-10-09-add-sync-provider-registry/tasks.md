## 1. Sync-provider registry

- [x] 1.1 Move custody's provider evidence into a `sync-providers` registry pack with an add-only vault overlay.
  Evidence: `tests/test_sync_provider_registry.py::test_an_owner_added_sync_folder_withholds_store_custody` goes red when custody reads only the shipped pack; `test_an_overlay_cannot_drop_a_shipped_provider` goes red when the adapter lets an overlay override a shipped entry. `tests/test_collection_store_s1_gate.py::test_unverified_custody_refuses_the_c_producer_and_leaves_a_b_usable` keeps shipped evidence refusing.
