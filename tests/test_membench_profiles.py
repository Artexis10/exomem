"""Profile factories: the embeddings profile differs from lexical by exactly
the embedding knob, so mislabeled runs (a lexical run labeled "embeddings")
cannot recur silently."""

from __future__ import annotations

from membench.adapters.exomem_local import embeddings_profile, lexical_profile


def test_embeddings_profile_flips_only_the_embedding_knob() -> None:
    lex = lexical_profile()
    emb = embeddings_profile()
    # Empty string, not "0": exomem's flag checks are string-truthiness, so
    # any non-empty value (including "0") would keep embeddings DISABLED.
    assert emb.settings["EXOMEM_DISABLE_EMBEDDINGS"] == ""
    assert lex.settings["EXOMEM_DISABLE_EMBEDDINGS"] == "1"
    differing = {
        key
        for key in set(lex.settings) | set(emb.settings)
        if lex.settings.get(key) != emb.settings.get(key)
    }
    assert differing == {"EXOMEM_DISABLE_EMBEDDINGS"}
    assert emb.name != lex.name


def test_profile_factories_are_deterministic() -> None:
    assert embeddings_profile().settings == embeddings_profile().settings
    assert lexical_profile().settings == lexical_profile().settings
