## 1. Serve an ordinary Records query from one snapshot

- [x] 1.1 Reuse one Records query session for source proof and row selection. Verify a concurrent append leaves the earlier query coherent.
- [x] 1.2 Route an ordinary query on an admitted collection beside the store thread. Compare filtered pages before and after at 5,000 and 100,000 generated rows.
- [x] 1.3 Verify the ordinary fallback and existing import status and typed query paths. Compare withheld and missing selectors with query readers at capacity.
- [x] 1.4 Keep the existing query deadline after link preparation. Verify a sourced append can commit while a read holds its snapshot.

## 2. Verify the released package

- [ ] 2.1 Check one public filtered query during a held store request on the released wheel with disposable state.
