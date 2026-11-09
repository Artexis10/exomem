## 1. Naming

- [x] 1.1 Add `page_names.resolve_page`, the short id and the ambiguity refusal, and one `title_key` (`tests/test_name_pages_by_title.py`).
- [x] 1.2 Route the 14 page parameters through `resolve_page`, and compare a short id in `expected_hash` in the leaf writers, after admission.
- [x] 1.3 Let `agent_refusal` pass candidate rows through as mappings.

## 2. Replies

- [x] 2.1 Name pages by title and id in released MCP replies (`Command.names_pages`), and add `id` to a page read.
- [x] 2.2 Update the scaffold references that describe replies, then regenerate the skill contract and the plugin copies.

## 3. Proof

- [x] 3.1 Prove the workflow end to end on a temporary vault: create, read by title, edit by id, stale id and its retry, ambiguity and its retry, and a restricted caller.
- [x] 3.2 Confirm the tool-surface digest and `tool_surface_contract.json` are unchanged.
- [ ] 3.3 After release, count title-not-found and ambiguity refusals and the retries each costs, on the benchmark session.
