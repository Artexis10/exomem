## 1. Select a fully tested source

- [x] 1.1 Add `release_cuts.py pick-source`, which returns the newest main commit since the last release with reusable Full evidence, or main's tip.
- [x] 1.2 Test both outcomes against a real Git history: a newer untested tip and an older green commit before the last release.
- [x] 1.3 Select the source with `pick-source` in `docs/release.md`.
