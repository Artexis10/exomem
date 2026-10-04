## 1. Landing gate

- [x] 1.1 Red-first tests for the gate, the command parser and the Codex shapes in `tests/test_capture_nudge_episode.py`; update `tests/test_nudge_diet.py` and `tests/test_hook_activation_mode.py` fixtures that assumed the length gate.
- [x] 1.2 Implement the landing gate in `src/exomem/_hooks/exomem_capture_nudge.py`; update its docstring.
- [x] 1.3 Regenerate the plugin copies with `exomem package-skills --plugin-root plugins/claude-code` and `scripts/cloud-plugin.py build`.
- [x] 1.4 `openspec validate --all --strict`; scoped hook tests; repository pre-push gates.
