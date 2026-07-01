# exomem sample vault

Tiny public vault for first-run smoke tests and documentation examples.

Run from the repo root:

```bash
uv run python scripts/smoke-sample-vault.py
```

The smoke is read-only and lean: it disables embeddings/media and verifies
`doctor`, keyword `find`, `get`, and `audit` against this sample.
