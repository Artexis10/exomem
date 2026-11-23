# Tasks

## 1. External surfaces in this repository

- [x] 1.1 Rename the distribution to `evermatter` in `pyproject.toml`; add the `evermatter` console script beside `exomem` and `kb`; point project URLs at the renamed repository and `evermatter.ai`.
- [x] 1.2 Build the same release under the `exomem` distribution name in the release workflow, and publish both names.
- [x] 1.3 Make the shipped install lines name `evermatter==<exomem-release>` and `uvx evermatter@<exomem-release>`; keep the packaging placeholder token.
- [x] 1.4 Change the MCP server title and instructions, CLI help and banners, and doctor and setup output to say Evermatter; keep the server key `exomem`.
- [x] 1.5 Rewrite the README, QUICKSTART, `docs/` and `TRADEMARK.md` for the new name; keep `EXOMEM_*` documented as the settings prefix.
- [x] 1.6 Change plugin and skill display names and descriptions, the scaffold's prose and `server.json`; keep their identifiers; regenerate the plugin trees and `docs/capabilities.md`.
- [ ] 1.7 Publish images as `ghcr.io/substrate-systems/evermatter*`; make the Cloud deploy verify each image against the repository its name identifies.

## 2. Repository and registries (operator steps marked)

- [ ] 2.1 Operator: create the PyPI pending trusted publisher for `evermatter`.
- [ ] 2.2 Move the repository to `substrate-systems/evermatter` in one move (licence change owner); operator: point the `exomem` trusted publisher at it.
- [ ] 2.3 Publish the new MCP registry entry; mark the old entry deprecated with a pointer to the new one.
- [ ] 2.4 Submit the display-name change to the ChatGPT app directory and the Claude directory.

## 3. Release

- [ ] 3.1 Agree the rename release with the release owner and with `adopt-fair-source-licence` section 3.
- [ ] 3.2 Cut the rename release; prove `pip install evermatter==<release>`, `uv tool upgrade exomem` from 0.200.0, and an image pull under both names.
- [ ] 3.3 Publish one statement for the rename and the licence change in the changelog, the README and the website.

## 4. Compatibility window (optional for archive: dated reviews tracked after the release)

- [ ] 4.1 On or after 2027-06-01, review image pulls and registry use under the old names; stop dual publishing only with evidence that they are unused.
- [ ] 4.2 On or after 2027-11-23, review the `exomem` PyPI downloads and the `exomem` command; decide on removal from that evidence.
