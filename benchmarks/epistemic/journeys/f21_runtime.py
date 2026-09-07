"""Seed the f21 corpus the way a person would: through the product's write path.

The content is :func:`epistemic.corpora.no_nudge.f21_runtime_pages`, which stays
a pure function of nothing. What this module adds is the *route*. Each origin is
captured as a Source through the capture leaf and then compiled into a note
through the note leaf — which is what the scenario's ``ingest_source`` op means —
and a real maintenance pass runs before anything is projected.

Writing the markdown bytes directly was measurably not the same state. The audit
reported ``frontmatter_compliance`` on all six pages and ``unprocessed_source``
on all three Sources, because a hand-written page carries neither the fields the
writer stamps nor the ``ingested_into:`` edge the note leaf appends to a Source
it consumed. Projecting that state would have measured the fixture's spelling
rather than the product, and a family whose whole point is "what does the runtime
do with an ordinary corpus" cannot afford the difference.

Deterministic and offline: no clock beyond the capture date the product stamps,
no network, and the vault is built from an empty directory rather than copied
from a sample, so nothing but f21's own content is ever in it.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..corpora import no_nudge as corpus

#: The capture kind. `session` is a shipped kind that declares no required URL,
#: which is what a synthetic corpus with no external artifact can honestly claim.
SOURCE_KIND = "session"


def _page_ref(path: object) -> str:
    """A vault page path as the wikilink body the product resolves."""

    return str(path).removeprefix("Knowledge Base/").removesuffix(".md")


def _wikilink(path: object) -> str:
    return f"[[{_page_ref(path)}]]"


class F21SeedRefused(RuntimeError):
    """A product leaf refused the synthetic corpus. Never silence, never a stub."""


def seed_runtime_vault(root: Path) -> Path:
    """Build one f21 vault through the product's own leaves and return ``root``.

    Raises :class:`F21SeedRefused` naming the leaf and the reason if any write is
    refused: a corpus the product will not accept is a finding about the corpus,
    not something to route around with a direct file write.
    """

    from exomem import commands, init, state_migration
    from exomem import schema as schema_module

    if not os.environ.get("EXOMEM_STATE_ROOT"):
        raise F21SeedRefused(
            "f21 runtime seed requires an explicit EXOMEM_STATE_ROOT outside the vault"
        )
    root = Path(root)
    init.init_vault(root)
    authority = state_migration.assert_offline_migration_authority(
        source="epistemic f21 real-runtime corpus seed"
    )
    state_migration.migrate_vault_state_offline(root, authority=authority)
    source_schema = schema_module.load_source_schema(root)

    previous_note: str | None = None
    for origin in corpus.f21_runtime_pages():
        try:
            added = commands.op_add(
                root,
                source_schema,
                content=origin.source_content,
                title=origin.source_title,
                source_kind=SOURCE_KIND,
                slug=f"f21-source-{origin.ordinal}",
            )
        except Exception as error:  # noqa: BLE001 - re-raised, never swallowed
            raise F21SeedRefused(
                f"capture leaf `add` refused origin {origin.ordinal}: {error}"
            ) from error
        body = origin.note_body
        if previous_note is not None:
            # The semantic contract refuses a compiled page that carries no
            # deliberate typed epistemic edge; only the first page in an empty
            # vault is exempt as `bootstrap`. A `sources:` reference does not
            # count — provenance is an excluded family, and a Source is a
            # connectable target rather than an eligible governed one. So each
            # note after the first supports the one before it: the corpus is
            # three readings of the same recurrence, which is exactly what the
            # edge says. It is added here rather than in the corpus because only
            # the write path knows where the product filed the previous note.
            body = f"{body}\n## Relations\n\n- supports [[{previous_note}]]\n"
        try:
            written = commands.op_note(
                root,
                content=body,
                note_type="insight",
                title=origin.note_title,
                slug=f"f21-context-{origin.ordinal}",
                sources=[_wikilink(added["path"])],
            )
        except Exception as error:  # noqa: BLE001 - re-raised, never swallowed
            raise F21SeedRefused(
                f"note leaf `note` refused origin {origin.ordinal}: {error}"
            ) from error
        previous_note = _page_ref(written["path"])

    maintenance_pass(root)
    return root


def maintenance_pass(root: Path) -> dict:
    """The scenario's `maintenance_pass` op, run for real.

    `reconcile` is the product's own maintenance leaf: it rebuilds the indexes
    and the derived state a fresh capture leaves stale. Running it is what makes
    the projected state the state an ordinary vault is in, rather than the state
    a just-written vault happens to be in mid-flight.
    """

    from exomem import commands
    from exomem import find as find_module

    find_module.clear_cache()
    result = commands.op_reconcile(Path(root))
    find_module.clear_cache()
    return result
