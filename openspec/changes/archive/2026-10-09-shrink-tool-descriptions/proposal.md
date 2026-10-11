## Why

The 32 product tool schemas are the largest per-session cost Exomem imposes on a coding agent. Many clients resend them on every turn, and prompt caching lowers what is paid but not the window they occupy. Measured on `main` at 0.97.0 (`scripts/measure-tool-schema-bytes.py`), the complete wire surface is **184,208 B** (compact JSON: description, input schema, output schema, annotations, title, `_meta`). The `shrink-bootstrap` measurements put the description-plus-input-schema part at 169,911 B; the same part re-measured here is 165,978 B on the fixture basis, and the difference is measurement method, not drift.

Roughly 60% of that is not guidance an agent needs on every turn:

- One 2,480 B "Semantic authoring" contract is pasted ten times: into five tool descriptions and into five parameter descriptions (`remember`, `replace_memory`, `edit_memory`, `observe_memory`, `manage_memory_file`). That is 24.8 KB, and the full contract is already served by `bootstrap(profile="full")` and the skill.
- `ask_memory` publishes a 16,045 B output schema (a deep typed union). Every other product tool publishes 45 B. The comment at `commands.py:6002` already records that this schema was kept untouched only to protect the fingerprint, and says to revisit it the next time the fingerprint moves for its own reasons. This change is that occasion.
- 18.6 KB is `anyOf [.., null]` wrappers and `"default": null` on 427 optional parameters, which restate "this parameter is optional".
- `activate_context` (5,479 B) and `maintain_memory` (2,795 B) carry reference-manual prose that belongs in skill references.
- `authorization_session_credential` is declared on all 32 tools for 6.7 KB, and `response_detail` on 21 for 4.4 KB.

## What Changes

- Reconcile the existing candidate with current main before delivery, preserving the newer API-scope, saved-engagement, source-preservation and file-handle rules. Historical size results are not measurements of the reconciled surface.
- Make the existing names usable without private harness instructions: distinguish current-turn context compilation from targeted retrieval, and teach action-specific Planning guards at the call site. Keep names unchanged in this delivery; `search_memory`/`get_context` are evaluation candidates, not approved renames or extra advertised aliases.
- Cut the complete local tool surface from 184,208 B to a ruled budget of **at most 90,000 B (-51%)** without removing any tool, parameter, enum value or refusal code.
- Replace the ten copies of the semantic-authoring contract with one ~740 B rule per authoring tool (five copies) and no copy in parameter descriptions. The contract digest is unchanged because the digest derives from the contract, not from its projection.
- Replace `ask_memory`'s typed output union with the wrapped loose object it would have if it were declared like `read_memory`, keeping the `result` wrap so structured-content shape does not move.
- Drop `"default": null` and the null arm of `anyOf` for optional parameters (subject to the ruling below).
- Shorten shared parameter descriptions once, in one place each, and stop restating enums and types in prose.
- Rewrite each remaining description around what a tool does, when to pick it over its neighbours, and the behaviour-critical rules the incidents depend on. Long lifecycle and mode catalogues move to `references/*.md` in the skill scaffold and to on-demand bootstrap sections.
- Add a test that pins the total wire bytes (and a per-tool ceiling) under the ruled budget, so the surface cannot silently regrow.
- Regenerate the schema fixture, the packaged tool-surface fingerprint, the hosted v5 candidate and `docs/capabilities.md`. Refresh cached external adapters through their supported paths afterwards; adapter-specific pending acceptance does not block independently verified product surfaces.
- Verify useful tool selection and legal calls through the installed public interface, separately from compiler relevance. A smaller schema is a measured property, not evidence by itself of better agent behaviour.

## Capabilities

### Modified Capabilities

- `command-surface`: the generated MCP surface gains a size budget alongside the existing byte-fidelity requirement.

## Impact

Touches `commands.py` (descriptions and parameter annotations), `command_surface.py` (the injected `response_detail` and `authorization_session_credential` parameters), `semantic_authoring.py` (`render_concise` projection), the `ask_memory` return annotation and `retrieval_models`, the skill scaffold references (generic text only), and the derived artifacts named in `tasks.md`. Hosted candidates v1 to v4 resolve pinned legacy schemas (`hosted_legacy_profile_schemas.json`) and `hosted-alpha-agent-v4-command-binding-v1` reuses the v4 profile unchanged, so none of them move. Hosted v5 renders from the live registry and does. Nothing here changes tool behaviour, authority, or stored data.
