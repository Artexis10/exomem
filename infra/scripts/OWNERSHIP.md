# Infrastructure script ownership

Owns deterministic validation, saved-plan inspection/apply, non-printing secret
handoff, and executable drill wrappers. Scripts default to read-only/dry-run,
never echo secrets, never apply an unsaved plan, and require exact per-resource
approval for any replacement or deletion.
