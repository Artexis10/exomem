## 1. Plugin archive

- [x] 1.1 Build the per-server archive from the packaged connector edition in `src/exomem/self_hosted_plugin.py`, validated by `_validate_claude_manifest`
- [x] 1.2 Force-include the generated connector edition archive in the wheel and list it in the sdist
- [x] 1.3 Serve `GET /claude-plugin.zip` on a server with a public origin
- [x] 1.4 Point `exomem setup --remote` at the download
- [x] 1.5 Cover the archive, the plugin name and the route in `tests/test_self_hosted_plugin.py`

## 2. Acceptance

- [ ] 2.1 Download the archive from a released self-hosted server, upload it in claude.ai, connect from the plugin's Connectors tab and observe a recall call
