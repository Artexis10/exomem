## 1. Edition

- [x] 1.1 Generate `claude-connector-hooks` from `plugins/cloud/definition.json` with hooks and skills and no MCP server
- [x] 1.2 List `evermatter-self-hosted` in `.claude-plugin/marketplace.json`
- [x] 1.3 Cover the edition in `tests/test_cloud_plugins.py` and the public-artifact privacy check
- [x] 1.4 Document the edition in `docs/cloud-plugin-distribution.md`
- [x] 1.5 Show the hooked edition as "Evermatter" and the connector edition as "Evermatter (self-hosted)", each description leading with the account it serves
- [x] 1.6 Refuse at build a Claude manifest whose description exceeds 500 characters or whose name breaks the lowercase-hyphen rule

## 2. Acceptance

- [ ] 2.1 Install `evermatter-self-hosted` on an account that connects Evermatter as a connector, start a fresh cloud session, and observe the hook's activation and a recall call without being asked
