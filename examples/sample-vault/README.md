# exomem sample vault

The sample vault now ships inside the package (`src/exomem/_sample_vault/`),
so the first-run proof works from a bare install — no clone needed:

```bash
uvx exomem demo
```

The demo is read-only and lean: it copies the bundled vault to a temp
directory, disables embeddings/media, and verifies `doctor`, keyword `find`,
`get`, and `audit` with per-step timings. Use `--keep` to keep the temp copy
and open it in Obsidian, `--json` for a machine-readable envelope (CI runs
exactly this).
