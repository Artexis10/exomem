## 1. Red Contract Tests

- [x] 1.1 Replace the coding-flavored example pins in `tests/test_portable_category_teaching.py`
      with cross-domain pins and add breadth assertions: exactly four lines, each parses
      to one valid unit via `semantic_units.parse_semantic_units`, at least two resolve
      `core`, at least one resolves `unregistered`, the union of category and tag tokens
      covers at least four distinct non-software domains plus exactly one `code` token,
      and every breadth line renders verbatim in `render_concise()`.
- [x] 1.2 Update `tests/test_semantic_authoring_contract.py`: new examples dict pin,
      breadth assertions in concise/expanded rendering, breadth added to the bounded
      tool-guidance exclusion list, `EXPECTED_NORMATIVE_IDENTITY` bumped to version 4
      with a placeholder digest, version literal updated.
- [x] 1.3 Update `tests/test_bootstrap.py` so full and compact profiles both pin
      `{role, domain, breadth}` (full additionally `rich`).
- [x] 1.4 Update the `v3 ` marker literals to `v4 ` in `tests/test_scaffold_no_leak.py`
      and `tests/test_workflow_skills.py`, and the rich-example wikilink target pin.
- [ ] 1.5 Run the focused suite and confirm red.

## 2. Contract Content

- [x] 2.1 Edit `_build_portable_categories()` in `src/exomem/semantic_authoring.py`:
      non-software `role` and `domain` examples, new four-line `breadth` set, life-domain
      rich Decision with identical feature coverage.
- [x] 2.2 Bump the contract version to 4 and render the breadth set in
      `render_concise()`; leave `render_tool_guidance()` and `bootstrap_projection()`
      structure untouched so tier-3 surfaces flow automatically.
- [x] 2.3 Recompute the normative identity and pin the real digest in
      `tests/test_semantic_authoring_contract.py`.

## 3. Scaffold Prose And Projection Sweep

- [x] 3.1 Add the "One contract, every domain" subsection with a fenced generic
      archetype example set to `src/exomem/_scaffold/_Schema/SKILL.md` outside the
      projected block.
- [x] 3.2 Re-project the rendered contract block byte-identically into the scaffold
      SKILL.md, all nine workflow skills, and `docs/semantic-language.md` between its
      generated markers, verifying the old block occurred exactly once per carrier.
- [x] 3.3 Regenerate plugin skills via `uv run exomem package-skills --plugin-root
      plugins/claude-code`.

## 4. Downstream Regeneration And Verification

- [x] 4.1 Regenerate `src/exomem/tool_surface_contract.json` and
      `tests/fixtures/mcp_tool_schemas.json` via `scripts/dump-tool-schemas.py`; record
      the new sha as pending in `deploy/chatgpt/personal-plugin-contract.json` with
      `refresh_required: true`.
- [x] 4.2 Regenerate `docs/capabilities.md` via `scripts/generate-capabilities.py` and
      verify with `--check`.
- [x] 4.3 Run the focused suite (teaching, contract, bootstrap, no-leak, workflow
      skills, plugin sync, schema fidelity, connector guardrails, tool surface, core
      pinning) to green.
- [x] 4.4 Run Ruff on changed files, the full lean pytest suite, and the latency gate;
      record verification evidence below.

## Closure evidence (T10 audit, 2026-10-09)

- PR #312 merged as 342f582f8 and shipped in v0.30.0. It changed every file these tasks name: the five test files, `semantic_authoring.py`, the scaffold and nine workflow skills, the plugin skills, `docs/semantic-language.md`, both schema fixtures, `docs/capabilities.md` and the ChatGPT contract (new sha pending).
- An independent code review returned APPROVE. The focused suite passed (75), and Ruff was clean. CI was green on "tests (py3.11)", "tests (py3.13)" (full suite with the latency gate), "capabilities doc" and "OpenSpec validation".
- 1.5 stays unticked: #312 does not record the red phase, and this audit runs no tests. Static check only: the base 342f582f8^ has no `breadth` key in `semantic_authoring.py` or in the teaching test.
- Later work changed the carriers, not this requirement: the contract version is now 6, and #1077 moved the "One contract, every domain" subsection out of the scaffold SKILL.md.
