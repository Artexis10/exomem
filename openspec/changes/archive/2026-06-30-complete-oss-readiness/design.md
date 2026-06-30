# Design — Complete OSS Readiness

## Decisions

- **Sample vault is committed under `examples/`.** It is separate from
  `tests/fixtures` so README commands can point at public-facing content without
  implying the test fixture is user documentation.
- **Smoke script is lean and read-only.** It imports kb-mcp directly, sets the
  same lightweight env gates used in tests, and verifies doctor/find/get/audit on
  the sample vault. It does not initialize, write, reconcile, or build embeddings.
- **Media remains optional.** The docs should make `uv sync --extra media` and
  Tesseract the explicit gate, with `doctor --profile media` as the diagnostic.
- **Release hygiene is checklist-first.** Do not automate PyPI or GitHub releases
  yet; define a repeatable maintainer path (`pytest`, ruff, OpenSpec, build,
  sample smoke, doctor profiles).
- **CI hardening stays cheap.** Add spec validation, sample smoke, and package
  build. Keep heavy media/model paths out of CI.

## Risks

- Sample vault drift: mitigate by testing it through the smoke script in CI.
- CI runtime: keep smoke keyword/BM25-only and avoid model extras.
- Remote docs can overpromise mobile: keep remote setup separate from local quickstart.
