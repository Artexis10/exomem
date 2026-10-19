## Why

A long-running service keeps the embedding matrix in memory only while it is used. The idle reaper frees it after 15 minutes, and a release cutover starts without it. The next recall then read every stored vector on its own request thread. On the personal service, 15 such loads took 2-203 s each, and the first ask after one cutover took 72.8 s. While the load ran, it held the index lock, so writers' cache updates and other recalls waited behind it.

## What Changes

- A managed recall that needs the in-memory matrix serves it only when it is resident and current, or when the bounded catch-up makes it current.
- Otherwise the request starts one background load, answers from its other lanes, and reports the dense lane as `warming` with reason `matrix_warming`.
- The background load reads the sidecar without the index lock. It takes the lock only to install and catch up the result.
- When the last background load failed, a managed recall reports the dense lane as `failed` and starts no load until a backoff ends. The backoff starts at seconds and doubles with each consecutive failure.
- An inline reader, such as an audit or a write advisory, waits for a running load instead of starting a second one.
- The startup warm uses the same loader. Startup order is unchanged.
- An offline caller with no activated runtime keeps the inline load.

## Capabilities

### Modified Capabilities

- `find-recall-efficiency`: a managed recall never loads the corpus matrix on its request thread.

## Impact

- Code: `src/exomem/embedding_index.py`, `src/exomem/find_candidates.py`, `src/exomem/recall_space.py`, and `src/exomem/server_runtime.py` (the startup warm's body only).
- Behaviour: the first recall after a cold or reaped matrix omits the dense lane and says so. Later recalls use the loaded matrix.
