## 1. Plugin archive

- [x] 1.1 Generate the `claude-server` template edition into `src/exomem/_plugins/` and check it in `scripts/cloud-plugin.py check`
- [x] 1.2 Personalise the template for the server's origin in `src/exomem/self_hosted_plugin.py`, validated by `_validate_claude_manifest`
- [x] 1.3 Serve `GET /claude-plugin.zip` on a server with a public origin
- [x] 1.4 Point `exomem setup --remote`, `docs/remote-quickstart.md` and `docs/deployment.md` at the download
- [x] 1.5 Cover the archive, the plugin name and the route in `tests/test_self_hosted_plugin.py`

## 2. Acceptance

- [ ] 2.1 Download the archive from a released self-hosted server, upload it in claude.ai, connect from the plugin's Connectors tab and observe a recall call
