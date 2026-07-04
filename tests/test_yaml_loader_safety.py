"""The hot-path YAML loader must stay on the SAFE schema.

`vault.yaml_safe_load` uses libyaml's CSafeLoader (~7x faster than the pure-
Python SafeLoader) — which is the C implementation of the SAME safe schema.
These tests pin that property so a future edit can never quietly widen the
loader to one that constructs arbitrary Python objects from `!!python/*` tags.
"""

from __future__ import annotations

import yaml

from exomem import vault


def test_hot_loader_is_a_safe_schema_loader() -> None:
    safe = {yaml.SafeLoader, getattr(yaml, "CSafeLoader", yaml.SafeLoader)}
    assert vault._YAML_SAFE_LOADER in safe, (
        "vault._YAML_SAFE_LOADER must be SafeLoader or CSafeLoader — never a "
        "loader that constructs arbitrary Python objects"
    )


def test_python_object_tags_do_not_construct() -> None:
    evil = (
        "---\n"
        'x: !!python/object/apply:os.system ["echo pwned"]\n'
        "---\n"
        "body\n"
    )
    fm, body, _ = vault.parse_frontmatter(evil)
    # ConstructorError (a YAMLError) → parse_frontmatter's guard returns {}.
    assert fm == {}
    assert body.startswith("body")
