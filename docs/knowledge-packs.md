# Knowledge packs

Knowledge packs are product-level guidance for Exomem. They let a fresh user pick
useful starting domains, and they let agents route simple intentions into the
right governed layer without asking the user to understand Sources, Evidence,
Notes, Entities, or supersession first.

Packs do not create a new storage engine. They compose Exomem's durable
primitives and typed tools.

## Built-in packs

- `legal-warranty` - receipts, disputes, insurance, contracts, deadlines, cases, and proof.
- `creative` - references, assets, drafts, productions, taste notes, and releases.
- `technical` - projects, repositories, decisions, failures, incidents, experiments, and runbooks.
- `health-athletic` - training, symptoms, measurements, protocols, injuries, goals, and health records.
- `business` - customers, meetings, commitments, contracts, invoices, risks, and decisions.
- `personal-records` - everyday documents, purchases, travel, home, vehicles, admin records, and life logistics.

`personal-records` is the default when Exomem cannot infer a more specific pack,
which makes it suitable for fresh or empty vaults.

## Schema

Built-in packs live in `src/exomem/packs/*.json`. The runtime loads them with
`importlib.resources`, validates them strictly, and exposes the schema in
`adopt` reports through `pack_schema`.

Each pack is a JSON object with these required fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable slug. |
| `name` | Human-readable pack name. |
| `description` | Short catalog description. |
| `purpose` | What the pack is for at product level. |
| `audience` | Who should choose it. |
| `beginner_description` | Plain-language onboarding copy. |
| `agent_instructions` | Routing guidance for agents; user-facing replies should stay simple. |
| `default_note_types` | Common compiled-note types for the pack. |
| `default_entity_types` | Common entity types for the pack. |
| `default_block_types` | Common conceptual blocks such as receipt, decision, protocol, or incident. |
| `suggested_folders` | Governed KB locations the pack commonly uses; selection does not create them. |
| `suggested_workflows` | Beginner-facing workflows with `title`, `intent`, `route`, and `example`. |
| `primitives` | Durable primitives the pack commonly uses. |
| `actions` | Simple actions the pack supports. Existing packs may use legacy product verbs `save`, `adopt`, `ask`, `prove`, `review`, `update`, `connect`; new packs may also use `remember`, `capture`, and `maintain`. |
| `examples` | User-facing prompts that should route to this pack. |
| `signals` | Structural tokens used by adoption scans to suggest the pack. |

Allowed primitives are `source`, `evidence`, `case`, `decision`, `record`,
`asset`, `production`, `entity`, `failure`, `pattern`, and `experiment`.

Unknown pack fields are rejected. Unknown workflow fields are also rejected. This
is intentional: a deployment should not believe a pack field is active when this
Exomem version ignores it.

## Selection manifest

Setup persists selected packs at:

```text
Knowledge Base/_Packs/selected-packs.json
```

Example:

```json
{
  "schema_version": 1,
  "selected_pack_ids": ["personal-records"],
  "source": "setup",
  "updated": "2026-07-07",
  "packs": [
    {
      "id": "personal-records",
      "name": "Personal records",
      "beginner_description": "Use this as the starter pack for everyday documents, purchases, travel, home, vehicles, and personal admin.",
      "agent_instructions": "Use this as the default when no stronger domain pack is selected...",
      "suggested_workflows": [],
      "actions": ["save", "ask", "prove", "review", "update"]
    }
  ]
}
```

Selection is guidance only. It does not create folders, migrate content, rewrite
old notes, or compile material. Setup writes the manifest only after the
`Knowledge Base/` scaffold exists.

## Adoption and setup behavior

Pack suggestions are deterministic. Exomem looks at structural signals from
`overview`: folder paths and sample file names. It does not read every note body
or ask a model to classify the vault.

The safe loop is:

1. Run `exomem setup` or `adopt(mode="scan-only")`.
2. Review suggested packs or choose explicit packs for a fresh vault.
3. Persist selected packs under `Knowledge Base/_Packs/`.
4. Optionally save an adoption manifest under `Knowledge Base/_Adoption/`.
5. Copy selected legacy files as Sources when provenance matters.
6. Compile selected material later, with citations.

## Mapping to typed tools

Packs do not bypass governance. They help agents choose existing typed tools:

| Simple action | Typed operation |
| --- | --- |
| `capture` raw material | `add`; use `preserve` or upload for Evidence |
| `remember` durable conclusion | `note` or `link`; use `replace` for supersession |
| `ask` | `find`, optionally `get` or `find(pack=true)` |
| `review` | `attention`, `audit`, `propose_compilation` |
| `connect` | `suggest_links`, `suggest_relations`, `graph_context`, `link` |
| `adopt` | `adopt(mode="scan-only")` before copy/compile modes |
| `maintain` | `audit`; explicit `audit_fix` or `reconcile` for repairs |

Agents should speak in product language. The user can say "save this warranty
receipt"; the agent chooses the evidence/proof route and reports the saved path.
