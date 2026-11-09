## 1. Forms
- [x] 1.1 Declare the typed tools and the two doors in `public_surface`, with one wire model and one translation per typed tool.
- [x] 1.2 Map every door operation to one operation entry, and prove at startup that each entry has exactly one public home.
- [x] 1.3 Generate the door schemas and the `read_instructions(topic)` help from the operation entries.

## 2. Guards
- [x] 2.1 Add unit ids to the observation leaf, with the version compared after admission.
- [x] 2.2 Add row ids to the records and plans leaves, and fill the container hash under the writer lease.
- [x] 2.3 Derive the vocabulary item for `accept`, the draft lookup and the `unlinked_reason` binding.

## 3. Leaves
- [x] 3.1 Let the download leaf take a list of pages.
- [x] 3.2 Let the page parameters of notes, sources and links take a title or an id.
- [x] 3.3 Drop the two duplicate review modes, and add `topic` to the instructions leaf.

## 4. Switch
- [x] 4.1 List the 23 tools, refuse every unlisted name, and remove the legacy aliases.
- [x] 4.2 Compare the Cloud artifact grant with the called tool.
- [x] 4.3 Keep both Cloud transfer tools listed on Cloud.

## 5. Text and contracts
- [x] 5.1 Rewrite the server instructions, the bootstrap routes and the hook nudges for the new names.
- [x] 5.2 Replace the capture hook's tool-name regex with the generated write stamp.
- [x] 5.3 Migrate the scaffold skill, docs, specs and tests to the new names.
- [x] 5.4 Rewrite `scripts/e2e_product_loop.py` as the proof of the new surface.

## 6. Generators and budgets
- [x] 6.1 Rerun the schema, capabilities, skill-contract, plugin, Cloud-plugin and harness-module generators.
- [x] 6.2 Set the byte budget from `measure-tool-schema-bytes.py`, and update the ChatGPT connector contract.

## 7. Delivery
- [ ] 7.1 Run the proof, the full corpus, the privacy gate and `openspec validate --all --strict`.
- [ ] 7.2 After release, refresh the ChatGPT draft and rescan the directory listing.
