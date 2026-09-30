"""The structured-collection store (OpenSpec ``move-structured-collections-to-sqlite``).

Dark until the GA gate: nothing routes collection reads or writes here yet,
and file mode is unchanged. ``schema`` owns the DDL and migrations,
``connection`` the writer and reader connections and readiness, ``chain``
the store-wide head, ``tokens`` the guard tokens and ``types`` the collection
type registry.
"""
