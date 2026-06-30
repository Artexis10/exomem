## Why

The first install-readiness slice made setup diagnosable (`doctor`) and made `uv`
the documented default. The remaining OSS-readiness gap is adoption polish: a
new user still needs a deterministic sample vault, a smoke-test loop, sharper
media/remote guidance, and a release checklist that keeps public builds boring.

## What Changes

- Add a small public sample vault and a read-only smoke script that proves the
  lean path works without model downloads.
- Tighten media install readiness: clearer Tesseract/media-extra docs and doctor
  remediations for the common Windows failure.
- Add release/packaging hygiene: a public release checklist and CI build/spec
  validation checks.
- Split remote setup into a friendlier checklist that uses `doctor --profile remote`
  as the validation gate.

Out of scope: Docker images, PyPI publishing automation, hosted control-plane, or
new server capabilities.

## Capabilities

### Modified Capabilities
- `install-readiness`: extend from local doctor/uv setup into sample-vault smoke,
  media readiness, release hygiene, and remote setup validation.

## Impact

- Docs/examples/scripts only, plus small CI additions.
- No MCP/REST schema changes and no vault migration.
- Tests remain lean; media smoke remains explicit/optional.
