"""Store mode: a vault whose pages the collection store owns (OpenSpec ``make-vault-store-canonical``).

``mode`` answers whether a vault is in store mode, and ``ownership`` names the files that mode owns.
Migration, rollback, the reconcile sweep and the projector all take these two answers from here.
"""
