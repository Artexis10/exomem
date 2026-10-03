## 1. Short Stop-hook blocks

- [x] 1.1 Red-first: lower the byte ceilings, expect the short form on the first fire, and pin the preamble and the pointer in `tests/test_nudge_diet.py`.
- [x] 1.2 Remove `REMINDER` and the first-fire branch from `src/exomem/_hooks/exomem_capture_nudge.py`; shrink the preamble, `REMINDER_SHORT` and `EPISODE_ASK`; fix the `_rearm_nudges` docstring.
- [x] 1.3 Drop the hook as a doctrine carrier from the carrier-pair, adoption, entity-lifecycle and baseline tests (the skill and engagement texts keep their pins); update `scripts/context-footprint.py`.
- [x] 1.4 Regenerate the plugin copies with `exomem package-skills --plugin-root plugins/claude-code` and `scripts/cloud-plugin.py build`; amend the `shrink-bootstrap` delta.
- [x] 1.5 `openspec validate --all --strict`; scoped hook and packaging tests; public-artifact privacy gate.
