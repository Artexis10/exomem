## 1. Keep enrolled Sources off the shared pages

- [x] 1.1 Skip the shared-page writes for a capture that starts Source custody, and stop creating fragment ownership in the renderer.
- [x] 1.2 Open a generated producer only in a vault whose catalogue already holds a shared-page Slot.
- [x] 1.3 Prove that an enrolled capture leaves the shared pages byte-identical and that a later unenrolled capture lists itself without Slots.
- [x] 1.4 Keep the legacy ownership tests on state that earlier builds wrote: trim, rotation, append, create, planning and a lost acknowledgement phase.
