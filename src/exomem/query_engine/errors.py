"""Query refusals, importable without the store libraries the query runtime loads."""

from __future__ import annotations


class QueryError(RuntimeError):
    """A bounded query refusal without physical store diagnostics."""

    def __init__(self, code: str, message: str = "collection query could not complete", *,
                 progress: dict | None = None) -> None:
        self.code = code
        self.message = message
        # A refusal that ends on its own reports how far it has come (QUERY_REBUILDING).
        self.progress = progress
        super().__init__(f"{code}: {message}")
