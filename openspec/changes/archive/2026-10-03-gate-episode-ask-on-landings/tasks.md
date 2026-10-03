## 1. Landing gate on the episode ask

- [x] 1.1 Red-first tests in `tests/test_capture_nudge_episode.py`; existing episode tests that asked without a landing now land.
- [x] 1.2 Implement `landed_since_ask` in `src/exomem/_hooks/exomem_capture_nudge.py`; update its docstring.
- [x] 1.3 Regenerate the plugin copies with `exomem package-skills --plugin-root plugins/claude-code` and `scripts/cloud-plugin.py build`; update `QUICKSTART.md` and `docs/prominence.md`.
- [x] 1.4 `openspec validate --all --strict`; scoped hook and packaging tests; public-artifact privacy gate.
