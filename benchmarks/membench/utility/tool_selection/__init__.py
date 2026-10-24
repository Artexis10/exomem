"""The paired tool-selection evaluation of the single-purpose tools pilot.

Compares the grouped, flat and nested MCP tool shapes of one build on Records
and Planning tasks. Real headless sessions run against a stdio server on a
seeded temporary vault; a task succeeds or fails by the vault's end state or by
a deterministic check of the answer. See ``openspec/changes/
expose-single-purpose-tools/design.md`` (D9) for the decision rule.
"""
