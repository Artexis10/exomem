## 1. Registry and tools (D1, D2, D3, D4)

- [x] 1.1 Add the closed `executor` attribute to the registry and the `CLOUD_GATEWAY_REQUIRED` refusal ahead of the read-only gate, the call ledger and the writer lease. Verify: `tests/test_cloud_cell.py` shows a direct call takes no lease, on a read-only cell too, and fails when the refusal is removed.
- [x] 1.2 Add `observe_transfer` and `transfer_memory` with their descriptions, annotations and closed schemas, and build the Cloud tool list in one registry function. Verify: `tests/test_cloud_cell.py` lists both tools on a Cloud cell and neither locally; `tests/test_preferred_command_names.py` and the bootstrap contract tests use the same function.
- [x] 1.3 List both commands in the governance coverage lists. Verify: `tests/test_authorization_request_context.py` and `tests/test_governance_egress.py` pass.
- [x] 1.4 Add the Cloud-only schema fixture and the Cloud surface budget. Verify: `tests/test_mcp_schema_fidelity.py` and `tests/test_tool_schema_budget.py` pass; the local fixture and fingerprint are unchanged.

## 2. Export download command (D6)

- [x] 2.1 Add `exomem cloud export`, sharing the import command's OAuth and endpoint helpers. Verify: `tests/test_cloud_export_client.py` shows a verified download, and no file after a tampered, truncated or oversized body, a non-https link or a redirect.

## 3. Gateway egress (D5)

- [x] 3.1 Add `cloudGateway.transferTools.enabled`, default `false`, with the 443 rule, the env variable, the guards and the schema entry. Verify: `infra/cellctl/tests/test_platform_chart_headers.py` shows the default render keeps egress 8765, 5432 and 53, the flag adds 443 with the private exceptions, and the chart refuses the flag without them.
- [x] 3.2 Amend the gateway-egress and Cloud-surface statements in `adopt-exomem-cloud-plain-cells` and `serve-cloud-artifact-handles`. Verify: `openspec validate --all --strict` passes.

## 4. Substrate and rollout

- [ ] 4.1 Substrate's change ships the gateway intercept for `observe_transfer` and `transfer_memory` behind `EXOMEM_CLOUD_TRANSFER_TOOLS_ENABLED`, and adds `archive_sha256` to the export display. Verify: Substrate's own tests, and a check that the tool names, actions and inputs equal `tests/fixtures/mcp_cloud_only_tool_schemas.json`.
- [ ] 4.2 Release acceptance in rehearsal: gateway image + transferTools.enabled + EXOMEM_CLOUD_EXPORTS_ENABLED, then handoff → CLI import → status during hold → adoption → export unconfirmed/confirmed → CLI download (owners: Cloud programme / deployment). The rehearsal values must first add every private range to `cells.jobEgressExcept`. Verify: the rehearsal evidence is recorded in this change.
