# Tasks — Complete OSS Readiness

## 1. Sample vault and smoke
- [x] Add `examples/sample-vault/` with a small valid Knowledge Base.
- [x] Add a lean read-only smoke script for doctor/find/get/audit.
- [x] Document expected sample smoke output in local setup docs.

## 2. Media and remote readiness docs
- [x] Tighten media install docs around `uv sync --extra media`, Tesseract, and doctor.
- [x] Add a compact remote setup checklist using `doctor --profile remote`.

## 3. Release and CI hygiene
- [x] Add release checklist docs.
- [x] Add CI jobs for OpenSpec validation, sample smoke, and package build.

## 4. Verify
- [x] Run focused smoke/tests.
- [x] Run full pytest, ruff on changed files, and OpenSpec validation.
