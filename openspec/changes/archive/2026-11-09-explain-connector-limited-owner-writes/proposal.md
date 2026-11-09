## Why

An owner agent that connects over a host connector boundary can often read a collection that the boundary's write ceiling stops it from writing. Every write it tries refuses `COLLECTION_NOT_FOUND` with no remediation, for a collection that its own query or inspect just returned. The agent then concludes the collection is missing, or goes around the tool. The refusal is right; the answer is wrong, because the connection already reads the collection and the error names nothing to change. A limited connection does not read every collection, so the explanation applies only where it reads the manifest; elsewhere the answer stays identical to an absent collection.

## What Changes

- An owner whose connection the connector boundary limits gets `COLLECTION_CONNECTOR_LIMITED` with a remediation when this connection reads the collection's manifest and only the boundary's write ceiling refuses the write. The remediation names where the boundary does not limit the write: the owner's CLI, a stdio connection, or a client whose boundary entry denies it no scope.
- The words of that refusal depend only on the manifest, so they reveal no withheld row.
- A limited owner whose write is refused for any other reason, including a manifest hidden from its connection, gets the same `COLLECTION_NOT_FOUND` as an absent collection.
- Owner inspect on such a connection says that a write carrying its lifecycle guards will be refused, because the guards cover only the rows the connection can read, and names where the boundary does not limit a write.
- Every other caller keeps the content-free `COLLECTION_NOT_FOUND`, identical to an absent collection. The refusal rule itself does not change.

## Capabilities

### Modified Capabilities

- `records`: the withheld-as-absent refusal names the connector limit to an owner whose connection reads the manifest and whose write only the boundary's write ceiling refuses.

## Impact

- Code: `src/exomem/collection_store/governance.py`, `src/exomem/collection_store/writer.py`, `src/exomem/governance/connector_boundary.py`, `src/exomem/record_memory.py`.
- Behaviour: only the error code, message and remediation that a limited owner connection sees change, and only on a manifest that connection reads. Guests and other audiences see no change.
