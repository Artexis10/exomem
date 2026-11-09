## Why

An owner agent that connects over a host connector boundary can read a collection but may not write it, because the boundary gives its connection no writable scope. Every write it tries refuses `COLLECTION_NOT_FOUND` with no remediation, for a collection that its own inspect just returned. The agent then concludes the collection is missing, or goes around the tool. The refusal is right; the answer is wrong, because the owner already knows the collection exists and the error names nothing to change.

## What Changes

- An owner whose connection the connector boundary limits gets `COLLECTION_CONNECTOR_LIMITED` with a remediation when a collection write is refused: make the write from the owner's CLI or a stdio connection, or give this client a boundary entry that can write the manifest and every row.
- The words of that refusal do not depend on whether any row is withheld, so they reveal no withheld row.
- Owner inspect on such a connection says that its lifecycle guards cover only the rows the connection can read and that no write accepts them.
- Every other caller keeps the content-free `COLLECTION_NOT_FOUND`, identical to an absent collection. The refusal rule itself does not change.

## Capabilities

### Modified Capabilities

- `records`: the withheld-as-absent refusal names the connector limit to an owner on a limited connection.

## Impact

- Code: `src/exomem/collection_store/governance.py`, `src/exomem/collection_store/writer.py`, `src/exomem/record_memory.py`.
- Behaviour: only the error code, message and remediation that a limited owner connection sees change. Guests and other audiences see no change.
