# Knowledge packs

Knowledge packs are declarative routing bundles. They help Exomem suggest how a messy vault might map onto durable primitives without hard-coding a new folder tree or asking users to understand the ontology first.

Built-in packs live in `src/exomem/packs/*.json`. The runtime loads them with `importlib.resources`, validates them strictly, and exposes the schema in `adopt` reports through `pack_schema`.

## Built-in packs

- `legal-warranty` — cases, receipts, correspondence, deadlines, and proof.
- `creative` — references, assets, drafts, productions, taste notes, and releases.
- `technical` — projects, repositories, decisions, failures, incidents, and runbooks.
- `health-athletic` — training, symptoms, measurements, protocols, injuries, and goals.
- `business` — customers, meetings, commitments, contracts, risks, and decisions.
- `personal-records` — purchases, travel, home, vehicles, admin records, and life logistics.

## Schema

Each pack is a JSON object with these required fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable slug. |
| `name` | Human-readable pack name. |
| `description` | One-sentence product description. |
| `primitives` | Durable primitives the pack commonly uses. |
| `actions` | Simple actions the pack supports: `save`, `adopt`, `ask`, `prove`, `review`, `update`, `connect`. |
| `examples` | User-facing prompts that should route to this pack. |
| `signals` | Structural tokens used by adoption scans to suggest the pack. |

Allowed primitives are `source`, `evidence`, `case`, `decision`, `record`, `asset`, `production`, `entity`, `failure`, `pattern`, and `experiment`.

Unknown fields are rejected. That is intentional: a deployment should not believe a pack field is active when this Exomem version ignores it.

## Example

```json
{
  "id": "legal-warranty",
  "name": "Legal / warranty",
  "description": "Cases, receipts, correspondence, deadlines, and proof.",
  "primitives": ["source", "evidence", "case", "decision", "record"],
  "actions": ["save", "prove", "review", "update"],
  "examples": [
    "Save this receipt for the laptop warranty case.",
    "Show the evidence for the landlord dispute."
  ],
  "signals": ["legal", "warranty", "receipt", "invoice", "contract"]
}
```

## Adoption behavior

Pack suggestions are deterministic. Exomem looks at structural signals from `overview`: folder names, sample file names, counts, and media mix. It does not read every note body or ask a model to classify the vault.

A pack suggestion is a proposed route, not a migration. The safe loop is:

1. Run `adopt(mode="scan-only")`.
2. Review suggested packs and actions.
3. Optionally save the manifest under `Knowledge Base/_Adoption/`.
4. Copy selected legacy files as Sources when provenance matters.
5. Compile selected material later, with citations.

## Mapping to typed tools

Packs do not bypass governance. They help agents choose existing typed tools:

| Simple action | Typed operation |
| --- | --- |
| Save raw material | `add` |
| Save durable conclusion | `note` or `link` |
| Preserve proof | `preserve` or upload to Evidence |
| Ask | `find` and `get` |
| Review | `audit`, `attention`, `propose_compilation` |
| Update | `edit`, `replace`, `reconcile` |
| Connect | `link`, `suggest_links` |
