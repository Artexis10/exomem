"""Process-local provenance for encrypted query continuations."""

_SEAL = object()


class _IssuedCursor(str):
    __slots__ = ()

    def __new__(cls, value: str, seal: object):
        if seal is not _SEAL:
            raise TypeError("only the query codec issues cursors")
        return str.__new__(cls, value)


def issued_cursor(value: str) -> str:
    """Mark ciphertext at its issuer; JSON input cannot carry this provenance."""
    return _IssuedCursor(value, _SEAL)


def is_issued_cursor(value: object) -> bool:
    return type(value) is _IssuedCursor
