"""Query refusals, importable without the store libraries the query runtime loads."""

from __future__ import annotations


class QueryError(RuntimeError):
    """A bounded query refusal without physical store diagnostics."""

    def __init__(self, code: str, message: str = "collection query could not complete") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
