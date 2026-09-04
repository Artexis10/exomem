## Context

`_publish_markdown_catalog_generation` builds its predecessor map as

```python
by_identity = {item.item_identity: item for item in active_namespace.items}
```

and then looks each mutation up by the relative path the mutation carries:

```python
predecessor = by_identity.get(relative)
if predecessor is None or predecessor.content_hash != mutation.expected_before_hash:
    raise CatalogPublicationError(
        "catalog content identity no longer matches the reviewed predecessor"
    )
```

Both arms of that condition produce the same message, so the observed failure is
either *no predecessor at that key* or *a predecessor whose hash differs*. The
two are worth separating before anything else, because they point at completely
different bugs — a key that does not match, versus content that does not.

## What has already been excluded

**Path separators.** The obvious hypothesis is a `\` key meeting a `/` key, and
the module already handles that: `str(path).replace("\\", "/")` and `.as_posix()`
appear at every construction site, and `relative` itself comes from
`write.path.absolute().relative_to(root).as_posix()`. This is not a naive
separator bug.

## The lead worth checking first

The module computes a normalized alias for each target —
`unicodedata.normalize("NFC", relative).casefold()` — but uses it **only** to
detect colliding membership targets. The `by_identity` lookup itself is an exact
match on the raw posix relative path.

That asymmetry is benign wherever the stored `item_identity` and the live path
text are byte-identical, which is the normal case on Linux. On Windows they need
not be: `relative` is derived from a live filesystem path, and casing there is
preserved but not significant, so a path that reaches publication through a
differently-cased or alias route yields a key the exact-match lookup misses
while the alias computation would have matched it.

This is a hypothesis, not a diagnosis. It has not been confirmed, and it does not
explain the `test_graph_epoch_protocol` failures, which may or may not share the
cause. `item_identity`'s producer has not been traced.

## Goals / Non-Goals

**Goals.** Establish which arm of the condition fires. Reproduce without a
Windows runner if possible. Fix the identity match so it holds on every declared
platform. Cover it where a pull request will see it.

**Non-Goals.** Widening the identity rule into case-insensitivity as a
convenience — if casing is the cause, the fix is to compare identities the way
they are minted, not to relax the comparison. Touching the advisory nature of the
nightly lane, which is deliberate.

## Risks / Trade-offs

**A relaxed comparison would hide a real collision.** The alias computation
exists to refuse colliding membership targets; reusing it as the lookup key
without care could let two distinct targets match one predecessor. Whatever the
fix, the collision refusal has to keep working.

**No Windows host here.** Everything above was read from source and from nightly
logs. A fix proposed without a reproduction is a guess, which is why task 1 is
the reproduction and not the patch.
