## 1. One publish path

- [x] 1.1 `plan` job: find the release (manifest version on a push, the input tag on a dispatch), check the release assets, PyPI, the MCP Registry entry and the six image tags, and decide: publish on the tag, hand off to the tag, or do nothing.
- [x] 1.2 Publish jobs build and publish only the missing artifacts; PyPI receives the wheel and sdist that `build-artifacts` built; a `dry_run` builds everything and publishes nothing.
- [x] 1.3 Remove `publish-existing-image` and `publish-existing-pypi`; keep the Records rollback input on the single path.
- [x] 1.4 Tests: every Dockerfile release target has a publish step that the plan checks; existing workflow pins follow the single path. `docs/release.md` describes the flow and the manual dispatch.
- [ ] 1.5 Dry run: a `dry_run` dispatch of the branch for `v0.115.0` builds all six images and publishes nothing.
- [ ] 1.6 Live: the next release publishes every artifact from a run on its tag, and a later push to `main` finds nothing missing.
