## Context

The canonical core and standalone skills already carry static operating and semantic rules. They still need current engagement, delegation ceilings and adapter capabilities. Fetching the generic compact contract duplicates that teaching in every fresh context.

## Goals / Non-Goals

**Goals:** provide a smaller live-state projection for skill-aware clients, preserve all dynamic authority and scope filtering, and keep old-server compatibility.

**Non-Goals:** change governance decisions, remove generic-client teaching, or infer monetary savings from serialized instruction size.

## Decisions

Add `profile="session"` as an opt-in projection of the existing filtered compact result. A client presents the SHA-256 `skill_contract` stamped into the canonical core and standalone skill metadata. The digest is canonical JSON over the LF-normalized core skill, routed references, and workflow skills discovered from the manifest, with only the stamp itself removed before hashing. Resolve that handshake before compact construction: absent or mismatched input returns the actual compact profile in the same call with a closed reason. Reuse the compact resolvers and filtering rather than maintaining a second authority lookup. Preserve server identity, active capabilities, engagement and envelope, governance, workflow contracts, relation currency, entity registry, source taxonomy, selected knowledge-pack guidance and its selection rule, and due state. Retain the compact due-state and family-disposition post-write cluster because it is authoritative runtime teaching. Omit the unselected catalogue, generic authoring contract, semantic recipes, examples, and the static workflow loop; the requested workflow label may be echoed separately when needed.

State in the result that the installed operating rules are a prerequisite and compact is the fallback. Existing compact/full/diagnostics outputs keep their contract. The skill fetches session state only when missing or after policy, connection, adapter, or returned vault configuration/registry state changes. If the exposed bootstrap schema lacks `skill_contract`, it requests compact directly; a server rejecting the new profile gets one compact fallback. Missing reference procedures still require the generic compact contract.

Use contract tests to compare retained fields to the compact result, test overridden/restricted surfaces and due-state propagation, and measure payload size on the same fixture. Keep the semantic grammar in every standalone authoring skill unchanged.

## Risks / Trade-offs

- A loaded skill can be older than its server. The digest handshake converts that mismatch into the portable compact contract; capability availability and live policy override static guidance. Unknown operations still require the current procedure or portable contract.
- Selected pack or custom vocabulary metadata can grow. Preserve that active state rather than silently truncating authority to meet a byte target.
- The projection still performs the existing deterministic local bootstrap work. This change reduces context transferred, without claiming to reduce server work or invoice cost.
