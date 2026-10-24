"""The structured-collection store (OpenSpec ``move-structured-collections-to-sqlite``).

A vault's Records and Planning collections live here once it migrates
(``exomem collections migrate``); until then file mode is unchanged.
``schema`` owns the DDL and migrations,
``connection`` the writer and reader connections and readiness, ``chain``
the store-wide head, ``tokens`` the guard tokens and ``types`` the collection
type registry.
"""
