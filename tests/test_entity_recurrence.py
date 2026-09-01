"""Entity recurrence sensor: an identity that recurs across notes is a candidate.

The corpus already records who and what a vault keeps coming back to — in the
wikilinks its notes carry. Nothing counts them. `resolve_entity_candidate` is
exact-match resolution against the registry, the audit reports an unresolved
wikilink page by page (`forward_reference`), and an identity linked from five
separate notes accumulates no signal anywhere.

These fixtures build the evidence the way a vault actually produces it: real
Markdown bodies with real wikilinks, swept through the real audit. Every gate
is exercised AT the module's own constant rather than at a literal restated
here, so moving a PROVISIONAL threshold moves the fixtures with it and the test
intent survives (design D2).
"""

from __future__ import annotations

from pathlib import Path

from exomem import audit as audit_module
from exomem import find as find_module

CATEGORY = "entity_recurrence"


# ------------------------------------------------------------------ vault fixtures


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find_module.clear_cache()
    return path


def _note(root: Path, rel: str, *, title: str, body: str) -> Path:
    """A compiled note whose body is the only evidence this sensor reads."""
    return _write(
        root,
        rel,
        f"---\ntype: insight\ntitle: {title}\nstatus: active\n---\n# {title}\n\n{body}\n",
    )


def _entity(
    root: Path,
    rel: str,
    *,
    title: str,
    aliases: list[str] | None = None,
    entity_type: str = "person",
    status: str = "active",
) -> Path:
    alias_line = f"aliases: [{', '.join(aliases)}]\n" if aliases else ""
    return _write(
        root,
        rel,
        f"---\ntype: entity\ntitle: {title}\nentity_type: {entity_type}\n"
        f"status: {status}\n{alias_line}---\n# {title}\n",
    )


def _findings(root: Path) -> list:
    report = audit_module.audit(root, categories=[CATEGORY])
    return [f for f in report.findings if f.category == CATEGORY]


#: The D6.1 corpus: three distinct notes reaching for one identity the vault has
#: never written down. Three is the spec scenario's own number, not a restatement
#: of the spread constant — `test_spread_gate_fires_exactly_at_the_constant`
#: below is what pins behaviour AT `SPREAD_MIN_PAGES`.
_RECURRING = "Marin Osk"
_MENTIONING = (
    "Knowledge Base/Notes/harbour-review.md",
    "Knowledge Base/Notes/interviews.md",
    "Knowledge Base/Notes/Patterns/handover.md",
)


def _recurring_vault(root: Path, *, target: str = _RECURRING) -> None:
    """Three notes link one identity; the registry holds two lexical neighbours."""
    for index, rel in enumerate(_MENTIONING):
        _note(
            root,
            rel,
            title=f"Note {index}",
            body=f"The handover went through [[{target}]] again.",
        )
    _entity(root, "Knowledge Base/Entities/People/marin-vale.md", title="Marin Vale")
    _entity(
        root,
        "Knowledge Base/Entities/Organizations/osk-yard.md",
        title="Osk Yard",
        entity_type="organization",
    )


# ------------------------------------------------------- 1.1 the acceptance fixture


def test_recurring_unresolved_identity_becomes_one_candidate(tmp_path: Path) -> None:
    """D6.1 — the whole point: three notes, one identity, one candidate finding."""
    _recurring_vault(tmp_path)

    findings = _findings(tmp_path)

    from exomem import entity_recurrence as sensor

    assert len(findings) == 1
    finding = findings[0]
    assert finding.category == CATEGORY
    assert finding.severity == "info"
    # Anchored on the lexicographically smallest mentioning page (design D4).
    assert finding.path == min(_MENTIONING)
    assert finding.meta["reasons"] == [sensor.REASON_UNRESOLVED_IDENTITY_RECURS]
    assert finding.meta["candidate"] == _RECURRING
    assert finding.meta["identity"] == sensor.identity_key(_RECURRING)
    assert finding.meta["pages"] == sorted(_MENTIONING)
    # The page list rides `meta` and NOT the fingerprint-bearing `paths` group
    # field, so spread growth cannot re-raise a settled candidate (design D4).
    assert finding.paths is None
    # The check-before-create assist: registry entries sharing an identity token.
    # One shared token each, so the tie breaks on path (design D3).
    assert [match["path"] for match in finding.meta["near_matches"]] == [
        "Knowledge Base/Entities/Organizations/osk-yard.md",
        "Knowledge Base/Entities/People/marin-vale.md",
    ]
    assert [match["shared_tokens"] for match in finding.meta["near_matches"]] == [
        ["osk"],
        ["marin"],
    ]


# ------------------------------------------------------------- 1.2 the quiet twins


def test_plain_text_mentions_at_matched_frequency_stay_quiet(tmp_path: Path) -> None:
    """D6.2 — the deferred stream, asserted so v1's scope is pinned, not implied.

    Same identity, same page count, same sentence — the wikilinks removed. The
    proper-n-gram stream is exactly the incidental-mention false-positive surface
    f21's budgets freeze behind calibration, so it must produce nothing here.
    """
    for index, rel in enumerate(_MENTIONING):
        _note(
            tmp_path,
            rel,
            title=f"Note {index}",
            body=f"The handover went through {_RECURRING} again.",
        )
    _entity(tmp_path, "Knowledge Base/Entities/People/marin-vale.md", title="Marin Vale")

    assert _findings(tmp_path) == []


def test_registry_resolved_alias_stays_quiet_at_five_pages(tmp_path: Path) -> None:
    """D6.3 — a resolved identity is the registry's business, not a candidate.

    Five pages, comfortably past the spread gate, linking a name the registry
    already answers to through an ALIAS rather than a title.
    """
    for index in range(5):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body=f"Reviewed with [[{_RECURRING}]] on Tuesday.",
        )
    _entity(
        tmp_path,
        "Knowledge Base/Entities/People/marin.md",
        title="Marin Oskarsdottir",
        aliases=[_RECURRING],
    )

    assert _findings(tmp_path) == []


def test_below_spread_stays_quiet(tmp_path: Path) -> None:
    """D6.4 — one page short of the gate says nothing, however emphatic it is."""
    from exomem import entity_recurrence as sensor

    for index in range(sensor.SPREAD_MIN_PAGES - 1):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body=f"[[{_RECURRING}]] again, and [[{_RECURRING}]] once more.",
        )

    assert _findings(tmp_path) == []


def test_spread_gate_fires_exactly_at_the_constant(tmp_path: Path) -> None:
    """The gate is pinned AT `SPREAD_MIN_PAGES`, in both directions.

    Frequency inside one page is never spread (the spec's second scenario): the
    below-gate corpus links the identity repeatedly on every page it has and is
    still silent, so only the page COUNT can be what flips this.
    """
    from exomem import entity_recurrence as sensor

    def corpus(pages: int) -> Path:
        root = tmp_path / f"vault-{pages}"
        for index in range(pages):
            _note(
                root,
                f"Knowledge Base/Notes/note-{index}.md",
                title=f"Note {index}",
                body=f"[[{_RECURRING}]] and again [[{_RECURRING}]].",
            )
        return root

    assert _findings(corpus(sensor.SPREAD_MIN_PAGES - 1)) == []
    assert len(_findings(corpus(sensor.SPREAD_MIN_PAGES))) == 1


def test_frequency_inside_one_page_is_not_spread(tmp_path: Path) -> None:
    """The spec's second scenario, and the per-page dedup rule it exists for.

    Five links on one page and one on another is six mentions and TWO pages. If
    dedup is what is holding this quiet rather than the spread gate, deleting it
    makes six clear a gate of three and this fires.
    """
    _note(
        tmp_path,
        "Knowledge Base/Notes/emphatic.md",
        title="Emphatic",
        body=" ".join(f"[[{_RECURRING}]]" for _ in range(5)),
    )
    _note(
        tmp_path,
        "Knowledge Base/Notes/passing.md",
        title="Passing",
        body=f"Also [[{_RECURRING}]].",
    )

    assert _findings(tmp_path) == []


# --------------------------------------------------- 2.1 what a wikilink identity is


def test_display_heading_extension_and_path_forms_are_one_identity(tmp_path: Path) -> None:
    """2.1 — `[[a|b]]`, `[[a#h]]`, `[[a.md]]` and `[[dir/a]]` name the same page.

    Four spellings, four pages, one candidate — and the display name is taken
    from the anchor page rather than from whichever form was scanned first.
    """
    forms = {
        "Knowledge Base/Notes/n0.md": f"[[{_RECURRING}|the harbourmaster]]",
        "Knowledge Base/Notes/n1.md": f"[[{_RECURRING}#history]]",
        "Knowledge Base/Notes/n2.md": f"[[{_RECURRING}.md]]",
        "Knowledge Base/Notes/n3.md": f"[[Knowledge Base/Entities/People/{_RECURRING}]]",
    }
    for index, (rel, body) in enumerate(sorted(forms.items())):
        _note(tmp_path, rel, title=f"Note {index}", body=body)

    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].meta["candidate"] == _RECURRING
    assert findings[0].meta["pages"] == sorted(forms)


def test_wikilinks_inside_code_are_not_evidence(tmp_path: Path) -> None:
    """A regex in a fenced block is not somebody reaching for a name."""
    for index in range(5):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body=f"Inline `[[{_RECURRING}]]` and\n\n```\n[[{_RECURRING}]]\n```\n",
        )

    assert _findings(tmp_path) == []


def test_folder_hubs_are_not_identities(tmp_path: Path) -> None:
    """A folder link names no page identity (design D1)."""
    for index in range(5):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body="[[Knowledge Base/Notes/Patterns/]]",
        )

    assert _findings(tmp_path) == []


def test_a_link_to_a_file_that_exists_is_an_attachment_not_an_identity(
    tmp_path: Path,
) -> None:
    """D2.2 — the FILE is what makes a suffixed link an attachment, not the dot.

    `_check_wikilinks` probes the filesystem before calling a suffixed link an
    attachment, and so does this. The scan is really there, so five notes
    reaching for it are reaching for a document, not for an unwritten identity.
    """
    scan = tmp_path / "Reference" / "scan-2026.pdf"
    scan.parent.mkdir(parents=True, exist_ok=True)
    scan.write_bytes(b"%PDF-1.4 fixture\n")
    for index in range(5):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body="[[Reference/scan-2026.pdf]]",
        )

    assert _findings(tmp_path) == []


def test_a_dot_in_a_name_is_punctuation_when_no_file_is_there(tmp_path: Path) -> None:
    """D2.2, the half a suffix heuristic got wrong.

    `Path("SomeProduct 2.0").suffix` is `.0`, and the same accident silenced
    `Dr. Ines Roth`, `U.S. Navy` and `Node.js` — four names a vault genuinely
    recurs on, each producing zero findings while the shipped link audit
    reported every one of them. Nothing stands at any of these paths, so each is
    a page identity and each recurs.
    """
    dotted = ["SomeProduct 2.0", "Dr. Ines Roth", "U.S. Navy", "Node.js"]
    for name in dotted:
        root = tmp_path / name.replace(" ", "-").replace("/", "-")
        for index in range(3):
            _note(
                root,
                f"Knowledge Base/Notes/note-{index}.md",
                title=f"Note {index}",
                body=f"Shipped with [[{name}]].",
            )
        findings = _findings(root)
        assert len(findings) == 1, f"{name!r} produced no candidate"
        assert findings[0].meta["candidate"] == name


# ------------------------------------------------------------ 2.1 the two exclusions


def test_links_from_inside_entities_count_nothing(tmp_path: Path) -> None:
    """D2.4 — the registry's own cross-links measure the registry, not attention.

    Two notes and one entity profile reach for the same unwritten name. Only the
    notes are the corpus paying attention, so this stays one page short of the
    gate; counting the profile would push it over.
    """
    for index in range(2):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body=f"Introduced by [[{_RECURRING}]].",
        )
    _write(
        tmp_path,
        "Knowledge Base/Entities/People/ines.md",
        "---\ntype: entity\ntitle: Ines Roth\nentity_type: person\nstatus: active\n"
        f"---\n# Ines Roth\n\nWorked with [[{_RECURRING}]].\n",
    )

    assert _findings(tmp_path) == []


def test_a_page_reaching_for_its_own_name_counts_nothing(tmp_path: Path) -> None:
    """D2.4 — a self-link has recurred with nobody.

    The link is the full-width spelling of the page's own title. NFKC folds the
    two together, so it IS the page's own identity; the resolver's exact-title
    map does not fold, so the link is genuinely unresolved and would otherwise be
    counted. Two ordinary notes carry the same spelling, leaving the corpus one
    page short of the gate unless the self-link is counted too.
    """
    wide = "Ｚｅｔａ"  # "Zeta", full-width
    _note(tmp_path, "Knowledge Base/Notes/zeta-note.md", title="Zeta",
          body=f"See [[{wide}]].")
    for index in range(2):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/other-{index}.md",
            title=f"Other {index}",
            body=f"See [[{wide}]].",
        )

    assert _findings(tmp_path) == []


# -------------------------------------------------- 2.2 registry resolution and assist


def test_only_an_active_registered_entity_page_resolves_an_identity(tmp_path: Path) -> None:
    """2.2 — each half of the registry predicate is load-bearing.

    Every variant below answers to the alias `Marin Osk` on paper and must not
    silence the sensor: a superseded profile, a page that is not an entity, a
    kind the registry does not know, a folder no registered kind owns, and the
    folder's index. Deleting any one of those checks admits that variant to the
    registry index and the candidate goes quiet.
    """
    variants = {
        "superseded": ("Knowledge Base/Entities/People/a.md",
                       "type: entity\nentity_type: person\nstatus: superseded\n"),
        "not an entity": ("Knowledge Base/Entities/People/b.md",
                          "type: insight\nentity_type: person\nstatus: active\n"),
        "unregistered kind": ("Knowledge Base/Entities/People/c.md",
                              "type: entity\nentity_type: spaceship\nstatus: active\n"),
        "unregistered folder": ("Knowledge Base/Entities/Vessels/d.md",
                                "type: entity\nentity_type: person\nstatus: active\n"),
        "folder index": ("Knowledge Base/Entities/People/index.md",
                         "type: entity\nentity_type: person\nstatus: active\n"),
    }
    for label, (rel, header) in variants.items():
        root = tmp_path / label.replace(" ", "-")
        for index in range(3):
            _note(
                root,
                f"Knowledge Base/Notes/note-{index}.md",
                title=f"Note {index}",
                body=f"Reviewed with [[{_RECURRING}]].",
            )
        _write(
            root,
            rel,
            f"---\n{header}title: Marin Oskarsdottir\naliases: [{_RECURRING}]\n"
            "---\n# Marin Oskarsdottir\n",
        )
        assert len(_findings(root)) == 1, f"{label} must not resolve the identity"


def test_a_registry_alias_silences_a_path_form_candidate(tmp_path: Path) -> None:
    """D2.3 — the half of the registry gate no wikilink resolver can reach.

    An entity page's TITLE is indexed by the resolver, so once the name-fallback
    exists the page gate silences a title match on its own. An ALIAS is indexed
    nowhere, which is what this gate uniquely answers for — asserted through a
    path form, so the link resolves to nothing either as written or by name.
    """
    for index in range(4):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body=f"Reviewed with [[Knowledge Base/People/{_RECURRING}]].",
        )

    assert len(_findings(tmp_path)) == 1

    _entity(
        tmp_path,
        "Knowledge Base/Entities/People/marin.md",
        title="Marin Oskarsdottir",
        aliases=[_RECURRING],
    )
    assert _findings(tmp_path) == []


def test_a_misfiled_path_form_does_not_invent_an_unwritten_identity(
    tmp_path: Path,
) -> None:
    """D2.2 — the link as written misses, but the NAME it ends in does not.

    `[[Knowledge Base/People/Marin Osk]]` while `Notes/Marin Osk.md` exists is a
    link filed under the wrong folder, and the audit already reports it as one.
    Reading it as "this identity is written down nowhere" would put a false
    sentence in front of the reader about a page they can see.
    """
    _note(tmp_path, f"Knowledge Base/Notes/{_RECURRING}.md",
          title=_RECURRING, body="The real page.")
    for index in range(3):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/n{index}.md",
            title=f"N{index}",
            body=f"See [[Knowledge Base/People/{_RECURRING}]].",
        )

    assert _findings(tmp_path) == []


def test_near_matches_are_capped_and_ordered_by_shared_tokens(tmp_path: Path) -> None:
    """D3 — bounded advice, ordered deterministically, strongest first."""
    from exomem import entity_recurrence as sensor

    for index in range(3):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body="Handled by [[Marin Osk Trust]].",
        )
    # Two shared tokens, then three with one each — more than the cap admits.
    _entity(tmp_path, "Knowledge Base/Entities/People/z-two.md", title="Marin Osk")
    _entity(tmp_path, "Knowledge Base/Entities/People/a-one.md", title="Marin Vale")
    _entity(tmp_path, "Knowledge Base/Entities/People/b-one.md", title="Osk Reyn")
    _entity(tmp_path, "Knowledge Base/Entities/People/c-one.md", title="Trust Board")

    matches = _findings(tmp_path)[0].meta["near_matches"]
    assert len(matches) == sensor.MAX_NEAR_MATCHES
    # Two shared tokens outrank one however late the path sorts; the rest tie on
    # count and fall back to path order.
    assert [match["path"] for match in matches] == [
        "Knowledge Base/Entities/People/z-two.md",
        "Knowledge Base/Entities/People/a-one.md",
        "Knowledge Base/Entities/People/b-one.md",
    ]


def test_an_ambiguous_bare_name_is_a_page_that_exists(tmp_path: Path) -> None:
    """D2.2 — a name the vault wrote down TWICE is not one it never wrote down.

    Two files share the stem, so the link resolves to a page that exists and
    merely needs disambiguating — which the audit already reports as a broken
    link rather than a forward reference. Treating ambiguity as absence would
    turn every colliding basename into an entity candidate.
    """
    _note(tmp_path, "Knowledge Base/Notes/a/Ghost.md", title="Ghost A", body="one")
    _note(tmp_path, "Knowledge Base/Notes/b/Ghost.md", title="Ghost B", body="two")
    for index in range(3):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/note-{index}.md",
            title=f"Note {index}",
            body="See [[Ghost]].",
        )

    assert _findings(tmp_path) == []


# --------------------------------------------------- 3.2 resolution by state change


def test_creating_the_entity_page_silences_it_and_deleting_it_brings_it_back(
    tmp_path: Path,
) -> None:
    """D6.5 — acting on the advice resolves it; undoing the act restores it."""
    _recurring_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    profile = _entity(
        tmp_path,
        "Knowledge Base/Entities/People/marin.md",
        title="Marin Oskarsdottir",
        aliases=[_RECURRING],
    )
    assert _findings(tmp_path) == []

    profile.unlink()
    find_module.clear_cache()
    assert len(_findings(tmp_path)) == 1, "no dismissal was recorded, so it returns"


def test_creating_the_linked_page_itself_silences_it_and_deleting_it_brings_it_back(
    tmp_path: Path,
) -> None:
    """D6.5, the other half — the link stops being unresolved at all."""
    _recurring_vault(tmp_path)
    assert len(_findings(tmp_path)) == 1

    target = _note(
        tmp_path,
        "Knowledge Base/Notes/marin-osk.md",
        title=_RECURRING,
        body="Notes on the handover.",
    )
    assert _findings(tmp_path) == []

    target.unlink()
    find_module.clear_cache()
    assert len(_findings(tmp_path)) == 1


def test_the_sweep_writes_nothing_at_all(tmp_path: Path) -> None:
    """"Advice, never creation" — the flagship no-nudge property, measured."""
    _recurring_vault(tmp_path)

    def snapshot() -> dict[str, bytes]:
        return {
            path.relative_to(tmp_path).as_posix(): path.read_bytes()
            for path in sorted(tmp_path.rglob("*.md"))
        }

    before = snapshot()
    assert len(_findings(tmp_path)) == 1
    assert snapshot() == before


# ------------------------------------------------------------------ 3.x delivery


def test_category_is_registered_and_triageable(tmp_path: Path) -> None:
    """3.1 — registered, selectable, disposable against; derivationally, not restated."""
    from exomem import attention as attention_module
    from exomem import review_state as review_state_module

    assert CATEGORY in audit_module.ALL_CATEGORIES
    assert CATEGORY in audit_module.EPISTEMIC_REVIEW_CATEGORIES
    assert CATEGORY in attention_module.ATTENTION_CATEGORIES
    assert CATEGORY in review_state_module.registered_families()
    # Opt-in: every candidate this sensor will ever name is already linked, so the
    # whole backlog arrives on the first run and must not displace the daily surface.
    assert CATEGORY not in attention_module.DEFAULT_ATTENTION_CATEGORIES


def test_fingerprint_binds_to_the_identity_not_to_any_page(tmp_path: Path) -> None:
    """D4 — the identity IS the signal.

    Editing a mentioning page, and even adding another one, leaves the signal
    version alone; a different identity gets a different one.
    """
    from exomem import entity_recurrence as sensor
    from exomem.vault import content_hash

    _recurring_vault(tmp_path)
    before = _findings(tmp_path)[0].meta["signal_version"]
    assert before == content_hash(sensor.identity_key(_RECURRING))[:16]

    _note(
        tmp_path,
        _MENTIONING[0],
        title="Note 0",
        body=f"The handover went through [[{_RECURRING}]] again, revised.",
    )
    _note(
        tmp_path,
        "Knowledge Base/Notes/fourth.md",
        title="Fourth",
        body=f"And again with [[{_RECURRING}]].",
    )
    after = _findings(tmp_path)[0]
    assert after.meta["page_count"] == 4
    assert after.meta["signal_version"] == before

    _recurring_vault(tmp_path / "other", target="Halle Berg")
    assert _findings(tmp_path / "other")[0].meta["signal_version"] != before


# -------------------------------------------------------------- 3.2 S6 integration


def _items(root: Path, *, state: str = "open") -> list:
    from exomem import attention as attention_module

    report = attention_module.attention(
        root, categories=[CATEGORY], limit=0, state=state, record_surfacing=False
    )
    return list(report.items)


def test_family_disposition_off_silences_the_queue(tmp_path: Path) -> None:
    """D6.6 — "stop suggesting entities at me" is obeyed."""
    from exomem import review_state as review_state_module

    _recurring_vault(tmp_path)
    assert len(_items(tmp_path)) == 1

    review_state_module.ReviewStateStore(tmp_path).set_disposition(
        CATEGORY, "off", why="intentional: I name entities when I choose to"
    )
    assert _items(tmp_path) == []


def test_a_dismissed_candidate_stays_dismissed_across_incidental_edits(
    tmp_path: Path,
) -> None:
    """D6.6 — the dismissal contract, end to end.

    "I have decided not to create this entity" binds to the identity, so editing a
    mentioning page must not resurrect it, and neither must a fourth page linking
    the same name: v1 defines no material-change reopen for spread growth (design
    D4, PROVISIONAL). A DIFFERENT identity is a different signal and still speaks.
    """
    from exomem import review_state as review_state_module

    _recurring_vault(tmp_path)
    item = _items(tmp_path)[0]
    review_state_module.apply_for_item(
        tmp_path, item, action="dismiss", why="intentional: a supplier, not an entity"
    )
    assert _items(tmp_path) == []

    _note(
        tmp_path,
        _MENTIONING[0],
        title="Note 0",
        body=f"The handover went through [[{_RECURRING}]] again, revised.",
    )
    assert _items(tmp_path) == [], "an incidental edit must not reopen a dismissal"

    _note(
        tmp_path,
        "Knowledge Base/Notes/fourth.md",
        title="Fourth",
        body=f"And again with [[{_RECURRING}]].",
    )
    assert _items(tmp_path) == [], "spread growth does not re-raise in v1"

    for index in range(3):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/halle-{index}.md",
            title=f"Halle {index}",
            body="Introduced by [[Halle Berg]].",
        )
    assert len(_items(tmp_path)) == 1, "a different identity is a different signal"


# ------------------------------------------- 2.1 what counts as present attention


def test_a_retired_page_neither_supplies_spread_nor_anchors(tmp_path: Path) -> None:
    """D2.5 — a superseded note records what the vault USED to reach for.

    Two live notes and one superseded one, and the superseded one sorts first, so
    without the rule it would both push the corpus over the gate AND become the
    page the finding names.
    """
    for index in range(2):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/n{index}.md",
            title=f"N{index}",
            body=f"Met [[{_RECURRING}]].",
        )
    _write(
        tmp_path,
        "Knowledge Base/Notes/aaa-old.md",
        f"---\ntype: insight\ntitle: Old\nstatus: superseded\n---\n# Old\n\n"
        f"Met [[{_RECURRING}]].\n",
    )

    assert _findings(tmp_path) == []

    # ...and the same page, live again, is evidence like any other.
    _note(tmp_path, "Knowledge Base/Notes/aaa-old.md", title="Old",
          body=f"Met [[{_RECURRING}]].")
    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].path == "Knowledge Base/Notes/aaa-old.md"


def test_an_excluded_page_neither_supplies_spread_nor_anchors(tmp_path: Path) -> None:
    """D2.5 — `excluded` means excluded, and anchoring is a surface too.

    The diary is placed so it sorts FIRST among the mentioning pages: while
    excluded it must neither push the corpus over the gate nor be nameable,
    and the moment it stops being excluded the anchor must land on it --
    pinning the spread half and the anchor half separately.
    """
    _write(tmp_path, "Knowledge Base/_access.yaml", "excluded:\n  - Aaa-private\n")
    for index in range(2):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/n{index}.md",
            title=f"N{index}",
            body=f"Met [[{_RECURRING}]].",
        )
    _note(tmp_path, "Knowledge Base/Aaa-private/aaa-diary.md", title="Diary",
          body=f"Met [[{_RECURRING}]] again.")

    assert _findings(tmp_path) == []

    # ...and the same page, no longer excluded, is evidence AND the anchor.
    _write(tmp_path, "Knowledge Base/_access.yaml", "excluded: []\n")
    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].path == "Knowledge Base/Aaa-private/aaa-diary.md"


# ------------------------------------------ 3.2 one review item per identity


def test_two_identities_sharing_an_anchor_are_triaged_independently(
    tmp_path: Path,
) -> None:
    """D4 — the identity partitions the review item, not just the fingerprint.

    Two identities recurring across one corpus routinely share an anchor: the
    page that sorts smallest mentions both. Fused onto one review id, a single
    dismissal puts down BOTH, and a third identity landing on that anchor changes
    the fused fingerprint and reopens the decision the reader already made. All
    three failures are measured here, in the order they bite.
    """
    _note(tmp_path, "Knowledge Base/Notes/aaa.md", title="AAA",
          body="[[Alpha One]] and [[Beta Two]]")
    for index in range(2):
        _note(tmp_path, f"Knowledge Base/Notes/b{index}.md", title=f"B{index}",
              body="[[Alpha One]]")
        _note(tmp_path, f"Knowledge Base/Notes/c{index}.md", title=f"C{index}",
              body="[[Beta Two]]")

    findings = _findings(tmp_path)
    assert {f.path for f in findings} == {"Knowledge Base/Notes/aaa.md"}
    assert sorted(f.meta["candidate"] for f in findings) == ["Alpha One", "Beta Two"]

    items = _items(tmp_path)
    assert len(items) == 2, "one anchor, two identities, two decisions to make"
    assert len({item.item_id for item in items}) == 2

    from exomem import review_state as review_state_module

    alpha = next(i for i in items if i.reasons[0]["meta"]["candidate"] == "Alpha One")
    review_state_module.apply_for_item(
        tmp_path, alpha, action="dismiss", why="intentional: a supplier, not an entity"
    )
    remaining = _items(tmp_path)
    assert [i.reasons[0]["meta"]["candidate"] for i in remaining] == ["Beta Two"], (
        "dismissing one identity must not put down the other"
    )

    # A third identity on the same anchor must not disturb either decision.
    _note(tmp_path, "Knowledge Base/Notes/aaa.md", title="AAA",
          body="[[Alpha One]] and [[Beta Two]] and [[Gamma Three]]")
    for index in range(2):
        _note(tmp_path, f"Knowledge Base/Notes/d{index}.md", title=f"D{index}",
              body="[[Gamma Three]]")
    after = sorted(i.reasons[0]["meta"]["candidate"] for i in _items(tmp_path))
    assert after == ["Beta Two", "Gamma Three"], (
        "an unrelated third identity reopened a settled dismissal"
    )


def test_opening_the_review_item_shows_the_mentioning_pages(tmp_path: Path) -> None:
    """The evidence a reader needs is the pages, and `meta` is where they live.

    Keeping the group out of `related_paths` protects the dismissal contract
    (design D4) but would otherwise leave the reviewer an item with no excerpts
    at all — the sentence "linked from three pages" and no way to see them.
    `review_context` draws group evidence from `meta["pages"]` for exactly this.
    """
    from exomem import review_context as review_context_module

    _recurring_vault(tmp_path)
    item = _items(tmp_path)[0]

    context = review_context_module.assemble(tmp_path, ref=item.ref)
    shown = [row["path"] for row in context["related"]["items"]]
    assert shown == sorted(_MENTIONING)[1:], "every mentioning page but the anchor"


def test_the_identity_key_helper_is_the_one_entity_candidates_owns(tmp_path: Path) -> None:
    """The two names this module borrows from `entity_candidates` are ITS names.

    `identity_key` is the shared normaliser and `_aliases` is the shared reader of
    the `aliases` frontmatter field. Both are imported rather than reimplemented,
    and `_aliases` is private — so a rename there must fail loudly here rather
    than leave this module quietly resolving no aliases at all.
    """
    from exomem import entity_candidates as entity_candidates_module
    from exomem import entity_recurrence as sensor

    assert sensor.identity_key is entity_candidates_module.identity_key
    assert sensor.alias_values is entity_candidates_module._aliases
    assert sensor.alias_values(["Ari", 3, "Vale"]) == ("Ari", "Vale")
    assert sensor.alias_values("Solo") == ("Solo",)
    assert tmp_path.exists()


# ------------------------------------------------------------- 4.4 determinism


_IDENTITIES = ("Marin Osk", "Halle Berg", "Ines Roth")


def _multi_identity_corpus(root: Path, *, reverse: bool = False) -> None:
    """Six notes, three recurring identities, several names per note."""
    plan = [
        (f"Knowledge Base/Notes/note-{index}.md", _IDENTITIES[index % 3], _IDENTITIES[(index + 1) % 3])
        for index in range(6)
    ]
    for rel, first, second in reversed(plan) if reverse else plan:
        _note(root, rel, title=rel, body=f"[[{first}]] met [[{second}]].")
    _entity(root, "Knowledge Base/Entities/People/marin-vale.md", title="Marin Vale")


def _shape(findings: list) -> list[tuple]:
    return [(f.category, f.severity, f.path, f.detail, f.meta) for f in findings]


def test_findings_are_identical_across_page_insertion_orders(tmp_path: Path) -> None:
    """D6.7 — nothing may depend on the order the corpus was written or walked."""
    from exomem import vault as vault_module

    forward_root, backward_root = tmp_path / "forward", tmp_path / "backward"
    _multi_identity_corpus(forward_root)
    _multi_identity_corpus(backward_root, reverse=True)
    assert _shape(_findings(forward_root)) == _shape(_findings(backward_root))
    assert len(_findings(forward_root)) == len(_IDENTITIES)

    # And the sweep itself, fed the same pages in the opposite order.
    pages = audit_module._parse_all(vault_module.kb_root(forward_root), forward_root)
    assert _shape(audit_module._check_entity_recurrence(forward_root, pages)) == _shape(
        audit_module._check_entity_recurrence(forward_root, list(reversed(pages)))
    )


# ------------------------------------------------------------------ 4.2 cost bound


def test_the_sweep_walks_the_vault_once(tmp_path: Path, monkeypatch) -> None:
    """4.2 — one path-only walk per sweep, whatever the corpus costs.

    The shape being prevented is a per-page existence walk, which would turn one
    sweep into a corpus scan per page. Counting the walk across a multi-page
    corpus is what makes "exactly one" provable rather than asserted — a per-page
    walk would track the page count. The other half, that the walk opens nothing,
    is `test_the_sweep_opens_only_the_files_it_can_name` below.
    """
    from exomem import vault as vault_module

    _recurring_vault(tmp_path)
    for index in range(12):
        _note(
            tmp_path,
            f"Knowledge Base/Notes/filler-{index}.md",
            title=f"Filler {index}",
            body=f"Nothing to see, and [[{_RECURRING}]].",
        )

    walks: list[int] = []
    original_walk = audit_module._walk_vault_md

    def counted(vault_root):
        walks.append(1)
        return original_walk(vault_root)

    def forbidden_resolver(self, vault_root):
        raise AssertionError("the sweep must not build a resolver from disk")

    monkeypatch.setattr(audit_module, "_walk_vault_md", counted)
    # The I/O-free `from_entries` seam is the whole point: `WikilinkResolver(root)`
    # re-reads and YAML-parses every Markdown file in the vault.
    monkeypatch.setattr(vault_module.WikilinkResolver, "__init__", forbidden_resolver)

    findings = _findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].meta["page_count"] == 15, "every page must actually be counted"
    assert len(walks) == 1, f"one vault walk per sweep, got {len(walks)}"


def _sweep_opens(tmp_path: Path, monkeypatch, root: Path) -> list[str]:
    """Every path the sweep itself opens, with the shared page parse excluded."""
    import builtins

    from exomem import vault as vault_module

    pages = audit_module._parse_all(vault_module.kb_root(root), root)
    opened: list[str] = []
    for owner, name in ((builtins, "open"), (Path, "read_text"), (Path, "read_bytes")):
        original = getattr(owner, name)

        def counting(first, *args, _original=original, **kwargs):
            opened.append(str(first))
            return _original(first, *args, **kwargs)

        monkeypatch.setattr(owner, name, counting)
    audit_module._check_entity_recurrence(root, pages)
    return opened


def test_the_sweep_opens_only_the_files_it_can_name(tmp_path: Path, monkeypatch) -> None:
    """4.2 — the sweep reads no page, and the files it DOES open are countable.

    The whole cost argument is that recurrence is counted over bodies the audit
    has already parsed. So the honest pin is not "zero I/O" — it is that every
    open is one this design can name. There is exactly one: the digest-cached
    entity-type registry. No page, no `Entities/` glob, no second corpus read.
    """
    root = tmp_path / "v"
    for index in range(8):
        _note(root, f"Knowledge Base/Notes/n{index}.md", title=f"N{index}",
              body=f"[[{_RECURRING}]]")

    opened = _sweep_opens(tmp_path, monkeypatch, root)

    assert [Path(path).name for path in opened] == ["entity-types.yaml"], opened


def test_a_dotted_candidate_costs_one_existence_probe_and_no_read(
    tmp_path: Path, monkeypatch
) -> None:
    """4.2 — the file probe added for dotted names is bounded and post-gate.

    It runs only for identities that already cleared spread and the registry, and
    it asks whether a file is there without reading one. Below the gate it never
    runs at all, which is what keeps a corpus full of `v1.2` mentions cheap.
    """
    from exomem import entity_recurrence as sensor

    root = tmp_path / "v"
    for index in range(sensor.SPREAD_MIN_PAGES):
        _note(root, f"Knowledge Base/Notes/n{index}.md", title=f"N{index}",
              body="Shipped [[Node.js]] and [[Some Plain Name]].")

    probed: list[str] = []
    original = audit_module._ordinary_file_exists

    def counting(vault_root, candidate):
        probed.append(str(candidate))
        return original(vault_root, candidate)

    monkeypatch.setattr(audit_module, "_ordinary_file_exists", counting)
    opened = _sweep_opens(tmp_path, monkeypatch, root)

    assert [Path(path).name for path in opened] == ["entity-types.yaml"], opened
    # Two spellings probed for the ONE dotted identity; the plain name costs none.
    assert [Path(path).name for path in probed] == ["Node.js", "Node.js"], probed


def test_a_below_gate_dotted_name_is_never_probed(tmp_path: Path, monkeypatch) -> None:
    """The probe is post-gate, so a name nobody recurred on costs nothing."""
    from exomem import entity_recurrence as sensor
    from exomem import vault as vault_module

    root = tmp_path / "v"
    for index in range(sensor.SPREAD_MIN_PAGES - 1):
        _note(root, f"Knowledge Base/Notes/n{index}.md", title=f"N{index}",
              body="Shipped [[Node.js]].")
    pages = audit_module._parse_all(vault_module.kb_root(root), root)

    probed: list[str] = []
    monkeypatch.setattr(
        audit_module,
        "_ordinary_file_exists",
        lambda vault_root, candidate: probed.append(str(candidate)) or False,
    )
    assert audit_module._check_entity_recurrence(root, pages) == []
    assert probed == []


def test_the_displayed_candidate_comes_from_the_anchor_page(tmp_path: Path) -> None:
    """D4 — the anchor decides the name shown, and nothing else does.

    Three pages write the same identity three ways, and the anchor page writes it
    twice. The name that reaches the reader is the smallest form on the
    lexicographically smallest mentioning page: no part of it may depend on which
    page or which mention the scanner reached first.
    """
    _note(tmp_path, "Knowledge Base/Notes/a.md", title="A",
          body="[[Marin Osk]] and later [[MARIN OSK]]")
    _note(tmp_path, "Knowledge Base/Notes/b.md", title="B", body="[[marin osk]]")
    _note(tmp_path, "Knowledge Base/Notes/c.md", title="C", body="[[Marin  Osk]]")

    finding = _findings(tmp_path)[0]
    assert finding.path == "Knowledge Base/Notes/a.md"
    assert finding.meta["candidate"] == "MARIN OSK"
    assert finding.meta["page_count"] == 3
