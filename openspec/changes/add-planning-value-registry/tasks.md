## 1. Registry

- [ ] 1.1 Add the `planning-values` pack, adapter and `RegistrySpec` with `class` and `parents`, and register the subject.
- [ ] 1.2 Validate the six governed fields through the registry in `normalize_item`, keep stored values readable, and pass the vault root at every call site.
- [ ] 1.3 Move the archive rule, the audit open-item rule and the parent-kind rule onto `class` and `parents`; record the remaining key rules as debt in the design.
- [ ] 1.4 Read the bootstrap lists, default horizon views and saved-view horizon check from the registry; keep frozen profiles on the shipped lists.

## 2. Proof and delivery

- [ ] 2.1 Workflow test through the tool surface: save a vault status, use it, see due-state and the archive rule classify it, restore, and read the stored item.
- [ ] 2.2 Run the Planning, audit, due-state, collection-store, bootstrap budget and frozen-profile suites unchanged.
- [ ] 2.3 Regenerate the skill contract, tool schemas, plugins, cloud plugin and hosted development render; run ruff, OpenSpec strict and the privacy gate.
