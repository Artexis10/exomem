## Decisions

- **One home.** `store_identity()` lives next to `cloud_mode_enabled` and reuses its flag parse. The hosted flag's truthy-set parse (`1`, `true`, `yes`, `on`) lives in `hosted_mode_enabled()`, which `resource_policy`, `preload_local_dotenv_policy` and `onnx_share_weights_enabled` call, so no other module holds a copy of that set. `privacy_log` and `prominence` still read `EXOMEM_HOSTED_CELL` themselves, because each uses the opposite convention: every value except an explicit false (`0`, `false`, `no`, `off`, empty) counts as hosted. `privacy_log` does this so a mistyped value fails toward content-private logging; `prominence` mirrors `mode._truthy`. Switching them to the truthy-set parse would change what a mistyped value does.
- **Closed set.** The three kinds are deployment modes the code implements, not vault vocabulary. The function branches on them directly and holds no word list.
- **Render time, not stored time.** `project_terminal` appends the name when it builds the response, from the live deployment. The stored terminal is unchanged, so a replay on the same deployment returns the same sentence and old receipts need no migration.
- **One place for the suffix.** The assignment of `compact["user_summary"]` appends the name. No verb branch does.
- **Carriers.** The session profile is what a client with the skill installed reads, so it carries `store`. The compact reference carries it too, in the `diagnostics_reading` section next to `server`.

## Open item

The always-served compact core cannot carry `store` without raising its byte ceiling or trimming other core text. Its populated worst case is 14,987 of 15,000 bytes, and `store` costs 52 to 58 bytes. This change raises no ceiling. A client without the skill therefore learns the store from `user_summary` and from `section=diagnostics_reading`, not from the core.
