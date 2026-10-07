from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import origin_bindings, provenance, semantic_index, source_closure
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.vault import PlannedWrite, batch_atomic_write

_ID = "12345678-1234-5678-1234-567812345678"
_REF = f"exomem://memory/{_ID}"
_PATH = "Knowledge Base/Sources/original.md"


def _write(
    vault: Path, body: str, *, relative: str = _PATH, metadata: str = "", identity: str = _ID
) -> dict:
    page = vault / relative
    page.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\ntype: source\nexomem_id: {identity}\n{metadata}---\n\n{body}"
    page.write_text(content, encoding="utf-8")
    return {
        "reference": f"exomem://memory/{identity}",
        "version": provenance.evidence_version(content),
    }


def test_exact_unit_and_subspan_keep_the_original_parent_root(vault: Path) -> None:
    """Different selected pieces do not manufacture origins or substitute nearby text."""
    binding = _write(
        vault, "- [finding] First α observation ^first\n- [finding] Second β observation ^second\n"
    )
    units = semantic_index.current_parent_index_state(vault, _PATH).document.units
    with request_scope(RequestPrincipal(audience_id="client-a")):
        page = origin_bindings.resolve_origin_input(vault, binding)
        selected = [
            origin_bindings.resolve_origin_input(
                vault,
                {
                    **binding,
                    "reference": unit.unit_ref,
                    "unit_fingerprint": unit.fingerprint,
                    "span": {"start_offset": 2, "end_offset": len(unit.span.text)},
                },
            )
            for unit in units
        ]
    assert {page.root, *(item.root for item in selected)} == {_REF}
    assert [item.text for item in selected] == [unit.span.text[2:] for unit in units]
    for item in selected:
        for guard in item.guards:
            guard.recheck(vault)


def test_backlinks_preserve_binding_but_body_and_lifecycle_edits_do_not(vault: Path) -> None:
    """Only writer bookkeeping is exempt from material version invalidation."""
    binding = _write(vault, "Original evidence.\n", metadata="status: active\n")
    with request_scope(RequestPrincipal(audience_id="client-a")):
        _write(
            vault, "Original evidence.\n", metadata="status: active\ningested_into: [compiled]\n"
        )
        assert origin_bindings.resolve_origin_input(vault, binding).root == _REF
        for body, metadata in (
            ("Changed evidence.\n", "status: active\n"),
            ("Original evidence.\n", "status: draft\n"),
        ):
            _write(vault, body, metadata=metadata)
            with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_STALE"):
                origin_bindings.resolve_origin_input(vault, binding)


@pytest.mark.parametrize("change", ["fingerprint", "span"])
def test_stale_unit_or_out_of_range_span_is_never_relocated(vault: Path, change: str) -> None:
    """An exact input selection cannot be clamped or rebound to a different unit."""
    binding = _write(vault, "- [finding] Exact observation ^exact\n")
    unit = semantic_index.current_parent_index_state(vault, _PATH).document.units[0]
    binding.update(reference=unit.unit_ref, unit_fingerprint=unit.fingerprint)
    if change == "fingerprint":
        binding["unit_fingerprint"] = "0" * 64
    else:
        binding["span"] = {"start_offset": 1, "end_offset": len(unit.span.text) + 1}
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_STALE"):
            origin_bindings.resolve_origin_input(vault, binding)


def test_compiled_page_cannot_pose_as_an_original(vault: Path) -> None:
    """A source type label outside the canonical original trees is not input proof."""
    binding = _write(vault, "Derived text.\n", relative="Knowledge Base/Notes/derived.md")
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            origin_bindings.resolve_origin_input(vault, binding)


def test_only_the_selected_exact_text_must_survive_terminal_filtering(vault: Path) -> None:
    """An adjacent credential does not contaminate a safe exact unit, or leak itself."""
    binding = _write(
        vault, "Authorization: Bearer synthetic-private-value\n- [finding] Safe observation ^safe\n"
    )
    unit = semantic_index.current_parent_index_state(vault, _PATH).document.units[0]
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            origin_bindings.resolve_origin_input(vault, binding)
        safe = origin_bindings.resolve_origin_input(
            vault,
            {
                **binding,
                "reference": unit.unit_ref,
                "unit_fingerprint": unit.fingerprint,
            },
        )
    assert safe.text == unit.span.text


def test_unchanged_bytes_are_not_authority_after_policy_narrows(vault: Path) -> None:
    """A retained byte proof cannot reuse a release verdict after its scope is narrowed."""
    from test_episode_recovery import _write_source_rule

    binding = _write(vault, "Original evidence.\n")
    _write_source_rule(vault, ceiling=6)
    rule = vault / "Knowledge Base/_Governance/rules/episode-source-client-a.yaml"
    with request_scope(RequestPrincipal(audience_id="client-a")):
        prior = origin_bindings.resolve_origin_input(vault, binding)
        rule.write_text(
            rule.read_text(encoding="utf-8").replace("ceiling: 6", "ceiling: 0"), encoding="utf-8"
        )
        prior.retained.guard.recheck(vault)
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            origin_bindings.resolve_origin_input(vault, binding)


def test_input_changed_during_terminal_check_cannot_return_a_proof(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last text check may wait; the input bytes still need checking after it."""
    binding = _write(vault, "Original evidence.\n")

    def change_during_check(root: Path, value: str) -> bool:
        _write(root, "Concurrent replacement.\n")
        return True

    monkeypatch.setattr(origin_bindings.retained_inputs, "exact_text_visible", change_during_check)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            origin_bindings.resolve_origin_input(vault, binding)


def _compiled(binding: dict) -> str:
    block = provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [],
            "bindings": [],
        }
    )
    return (
        "---\ntype: insight\nsources: ['"
        + _PATH.removesuffix(".md")
        + "']\n---\n\n"
        + block
        + "\n\n## Findings\n\n- [finding] Retain the observation.\n"
    )


def test_prepared_inputs_survive_the_real_source_closure_batch(vault: Path) -> None:
    """A valid backlink rewrite must not fail its own old-byte read-only guard."""
    binding = _write(vault, "Original evidence.\n", metadata="ingested_into: []\n")
    source = _compiled(binding)
    destination = "Knowledge Base/Notes/Insights/derived.md"
    with request_scope(RequestPrincipal(audience_id="client-a")):
        prepared = origin_bindings.prepare_origin_inputs(vault, source)
        closure = source_closure.prepare_source_closure(vault, source, destination=destination)
        writes = [*closure.backref_writes, PlannedWrite(vault / destination, source)]
        assert prepared is not None and closure.backref_writes
        batch_atomic_write(
            writes,
            vault_root=vault,
            required_guards=prepared.required_guards(vault, writes),
            _validate_prepared_bindings=lambda: prepared.revalidate(vault),
        )
        prepared.revalidate(vault)
    assert (vault / destination).read_text(encoding="utf-8") == source
    assert "derived" in (vault / _PATH).read_text(encoding="utf-8")


def test_batch_refuses_policy_narrowing_after_output_is_installed(vault: Path) -> None:
    """A final fresh policy check rolls back both the output and its Source backlink."""
    from test_episode_recovery import _write_source_rule

    binding = _write(vault, "Original evidence.\n", metadata="ingested_into: []\n")
    before = (vault / _PATH).read_text(encoding="utf-8")
    source = _compiled(binding)
    destination = "Knowledge Base/Notes/Insights/derived.md"
    output = vault / destination
    _write_source_rule(vault, ceiling=6)
    rule = vault / "Knowledge Base/_Governance/rules/episode-source-client-a.yaml"
    with request_scope(RequestPrincipal(audience_id="client-a")):
        prepared = origin_bindings.prepare_origin_inputs(vault, source)
        closure = source_closure.prepare_source_closure(vault, source, destination=destination)
        writes = [*closure.backref_writes, PlannedWrite(output, source)]
        assert prepared is not None and closure.backref_writes

        def validate() -> None:
            if output.exists():
                rule.write_text(
                    rule.read_text(encoding="utf-8").replace("ceiling: 6", "ceiling: 0"),
                    encoding="utf-8",
                )
            prepared.revalidate(vault)

        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            batch_atomic_write(
                writes,
                vault_root=vault,
                required_guards=prepared.required_guards(vault, writes),
                _validate_prepared_bindings=validate,
            )
    assert not output.exists()
    assert (vault / _PATH).read_text(encoding="utf-8") == before


def test_source_rewrite_cannot_change_evidence_under_a_backlink_exception(vault: Path) -> None:
    """The exception for an input's own write covers bookkeeping, not new evidence."""
    binding = _write(vault, "Original evidence.\n")
    with request_scope(RequestPrincipal(audience_id="client-a")):
        prepared = origin_bindings.prepare_origin_inputs(vault, _compiled(binding))
        assert prepared is not None
        proof = prepared.inputs[0][1]
        changed = PlannedWrite(
            vault / _PATH,
            proof.retained.page.content.replace("Original evidence", "Changed evidence"),
            guard=proof.retained.guard,
        )
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_STALE"):
            prepared.required_guards(vault, [changed])
    assert "Original evidence" in (vault / _PATH).read_text(encoding="utf-8")


def test_two_bound_recaps_and_a_unit_still_supply_one_owned_episode_root(vault: Path) -> None:
    """Repeated recorder inputs and selected units share the verified episode, not page IDs."""
    from exomem.episode_recovery import EpisodeInputOwner

    metadata = "source_type: episode\nepisode: shared-conversation\n"
    first = _write(vault, "- [finding] First retained observation ^first\n", metadata=metadata)
    second_path = "Knowledge Base/Sources/second-recap.md"
    second = _write(
        vault,
        "- [finding] Later retained observation ^later\n",
        relative=second_path,
        metadata=metadata,
        identity="87654321-4321-6789-4321-678987654321",
    )
    unit = semantic_index.current_parent_index_state(vault, second_path).document.units[0]
    with request_scope(RequestPrincipal(audience_id="client-a")):
        owner = EpisodeInputOwner(vault)
        owner.bind_committed_input("shared-conversation", path=_PATH, reference=first["reference"])
        owner.bind_committed_input(
            "shared-conversation", path=second_path, reference=second["reference"]
        )
        resolved = [
            origin_bindings.resolve_origin_input(vault, binding)
            for binding in (
                first,
                second,
                {**second, "reference": unit.unit_ref, "unit_fingerprint": unit.fingerprint},
            )
        ]
    assert len({proof.root for proof in resolved}) == 1
    assert all(proof.journal_guard is not None for proof in resolved)
    assert resolved[-1].text == unit.span.text
    with request_scope(RequestPrincipal(audience_id="client-b")):
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            origin_bindings.resolve_origin_input(vault, first)


def test_recaps_cannot_establish_a_root_from_labels_alone(vault: Path) -> None:
    """A readable Source with a plausible episode label is not a recorded original."""
    binding = _write(
        vault, "Unbound recap.\n", metadata="source_type: episode\nepisode: invented-label\n"
    )
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            origin_bindings.resolve_origin_input(vault, binding)


def test_note_authoring_refuses_a_stale_retained_origin_before_publication(vault: Path) -> None:
    """The ordinary writer must not publish new attribution to an old input version."""
    from exomem import note

    binding = _write(vault, "Original evidence.\n", metadata="ingested_into: []\n")
    content = (
        provenance.encode_origin(
            {
                "inputs": {"original": binding},
                "assessments": [],
                "bindings": [],
            }
        )
        + "\n\n## Findings\n\n- [finding] Preserve exact input versions.\n"
    )
    _write(vault, "Replacement evidence.\n", metadata="ingested_into: []\n")
    existing_notes = set((vault / "Knowledge Base/Notes/Insights").glob("*.md"))
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(note.NoteError) as failure:
            note.note(
                vault,
                content=content,
                note_type="insight",
                title="Bound input",
                status="draft",
                sources=[_PATH.removesuffix(".md")],
            )
    assert failure.value.code == "ORIGIN_INPUT_STALE"
    assert set((vault / "Knowledge Base/Notes/Insights").glob("*.md")) == existing_notes
    assert "ingested_into: []" in (vault / _PATH).read_text(encoding="utf-8")


def test_note_commits_backlink_and_edits_preserve_existing_stale_attribution(vault: Path) -> None:
    """A real create succeeds; editing its claim cannot rebind old attribution."""
    from exomem import edit, note

    binding = _write(vault, "Original evidence.\n", metadata="ingested_into: []\n")
    block = provenance.encode_origin(
        {"inputs": {"original": binding}, "assessments": [], "bindings": []}
    )
    with request_scope(RequestPrincipal(audience_id="client-a")):
        result = note.note(
            vault,
            content=block + "\n\n## Findings\n\n- [finding] Keep retained inputs exact.\n",
            note_type="insight",
            title="Bound writer workflow",
            status="draft",
            sources=[_PATH.removesuffix(".md")],
        )
        assert (vault / result.path).is_file()
        source = (vault / _PATH).read_text(encoding="utf-8")
        assert "bound-writer-workflow" in source
        assert provenance.evidence_version(source) == binding["version"]
        _write(vault, "Later evidence.\n", metadata="ingested_into: []\n")
        edited = edit.edit(
            vault,
            path=result.path,
            why="Clarify the finding without reauthoring its origin metadata.",
            old_string="Keep retained inputs exact.",
            new_string="Keep retained input versions exact.",
        )
        assert edited.path == result.path
    current = (vault / result.path).read_text(encoding="utf-8")
    assert "Keep retained input versions exact." in current
    assert (
        provenance.parse_origin(current, managed=True).payload
        == provenance.parse_origin(block, managed=True).payload
    )


def test_edit_refuses_new_origin_metadata_bound_to_stale_evidence(vault: Path) -> None:
    """Adding attribution on an existing page uses the same input gate as creation."""
    from exomem import edit, note

    binding = _write(vault, "Original evidence.\n")
    block = provenance.encode_origin(
        {"inputs": {"original": binding}, "assessments": [], "bindings": []}
    )
    with request_scope(RequestPrincipal(audience_id="client-a")):
        result = note.note(
            vault,
            content="## Findings\n\n- [finding] Keep retained inputs exact.\n",
            note_type="insight",
            title="Existing writer workflow",
            status="draft",
        )
        before = (vault / result.path).read_text(encoding="utf-8")
        _write(vault, "Later evidence.\n")
        with pytest.raises(edit.EditError) as failure:
            edit.edit(
                vault,
                path=result.path,
                why="Add exact input attribution.",
                new_body=block + "\n\n## Findings\n\n- [finding] Keep retained inputs exact.\n",
            )
    assert failure.value.code == "ORIGIN_INPUT_STALE"
    assert (vault / result.path).read_text(encoding="utf-8") == before


def test_note_rolls_back_if_input_permission_narrows_after_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production creation must wire the final policy gate, not just prepare inputs."""
    from test_episode_recovery import _write_source_rule

    from exomem import note
    from exomem import vault as vault_module

    binding = _write(vault, "Original evidence.\n", metadata="ingested_into: []\n")
    before = (vault / _PATH).read_text(encoding="utf-8")
    block = provenance.encode_origin(
        {"inputs": {"original": binding}, "assessments": [], "bindings": []}
    )
    _write_source_rule(vault, ceiling=6)
    rule = vault / "Knowledge Base/_Governance/rules/episode-source-client-a.yaml"
    output = vault / "Knowledge Base/Notes/Insights/late-policy-change.md"

    def narrow_after_install(destination: Path) -> None:
        if destination == output:
            rule.write_text(
                rule.read_text(encoding="utf-8").replace("ceiling: 6", "ceiling: 0"),
                encoding="utf-8",
            )

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", narrow_after_install)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(note.NoteError) as failure:
            note.note(
                vault,
                content=block + "\n\n## Findings\n\n- [finding] Keep retained inputs exact.\n",
                note_type="insight",
                title="Late policy change",
                status="draft",
                sources=[_PATH.removesuffix(".md")],
            )
    assert failure.value.code == "ORIGIN_INPUT_UNAVAILABLE"
    assert not output.exists()
    assert (vault / _PATH).read_text(encoding="utf-8") == before


def test_note_fills_the_exact_output_fingerprint_but_edits_never_rebind_it(vault: Path) -> None:
    """Public authoring binds one unit; later edits preserve rather than refresh its proof."""
    from exomem import edit, note

    binding = _write(vault, "Original evidence.\n")
    authored = {
        "inputs": {"original": binding},
        "assessments": [],
        "bindings": [{"inputs": ["original"], "scope": {"kind": "unit", "unit_ref": "#first"}}],
    }
    block = provenance.encode_origin(authored, authoring=True)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        result = note.note(
            vault,
            content=block + "\n\n## Observations\n\n- [finding] First claim ^first\n"
            "- [finding] Other claim ^other\n",
            note_type="insight",
            title="Exact output workflow",
            status="draft",
        )
        persisted = provenance.parse_origin(
            (vault / result.path).read_text(encoding="utf-8"), managed=True
        )
        assert persisted.status == "valid"
        scope = persisted.payload["bindings"][0]["scope"]
        original = semantic_index.current_parent_index_state(vault, result.path).document
        assert scope["fingerprint"] == original.units[0].fingerprint
        edit.edit(
            vault,
            path=result.path,
            why="Clarify a different claim.",
            old_string="Other claim",
            new_string="Clarified other claim",
        )
        unrelated = semantic_index.current_parent_index_state(vault, result.path).document
        assert provenance.match_origin_scope(scope, document=unrelated).status == "found"
        edit.edit(
            vault,
            path=result.path,
            why="Revise the bound claim without reauthoring its attribution.",
            old_string="First claim",
            new_string="Revised first claim",
        )
        changed = semantic_index.current_parent_index_state(vault, result.path).document
        assert provenance.match_origin_scope(scope, document=changed).status == "stale"
    assert (
        provenance.parse_origin(
            (vault / result.path).read_text(encoding="utf-8"), managed=True
        ).payload
        == persisted.payload
    )
    assert "fingerprint" not in authored["bindings"][0]["scope"]


def test_note_refuses_an_authored_wrong_output_fingerprint(vault: Path) -> None:
    """Supplying a fingerprint must not be treated like requesting an omitted one."""
    from exomem import note

    binding = _write(vault, "Original evidence.\n")
    block = provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [],
            "bindings": [
                {
                    "inputs": ["original"],
                    "scope": {"kind": "unit", "unit_ref": "#first", "fingerprint": "0" * 64},
                }
            ],
        }
    )
    with request_scope(RequestPrincipal(audience_id="client-a")):
        with pytest.raises(note.NoteError) as failure:
            note.note(
                vault,
                content=block + "\n\n## Observations\n\n- [finding] Exact claim ^first\n",
                note_type="insight",
                title="Wrong output fingerprint",
                status="draft",
            )
    assert failure.value.code == "ORIGIN_SCOPE_STALE"
    assert not (vault / "Knowledge Base/Notes/Insights/wrong-output-fingerprint.md").exists()


@pytest.mark.parametrize("layout", ["compact", "rich"])
def test_reviewed_note_commit_reproduces_the_normalized_origin_effect(
    vault: Path, layout: str
) -> None:
    """Omitted fingerprints must seal the same effect in validation and commitment."""
    from exomem import note

    binding = _write(vault, "Original evidence.\n")
    block = provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [],
            "bindings": [{"inputs": ["original"], "scope": {"kind": "unit", "unit_ref": "#claim"}}],
        },
        authoring=True,
    )
    content = (
        block + "\n\n## Observations\n\n- [finding] Exact reviewed claim ^claim\n"
        if layout == "compact"
        else "## Decision\n\n- id: claim\n\nExact reviewed claim.\n\n" + block + "\n"
    )
    arguments = {
        "content": content,
        "note_type": "insight",
        "title": "Reviewed origin workflow",
        "status": "active",
    }
    with request_scope(RequestPrincipal(audience_id="client-a")):
        prepared = note.note(vault, **arguments, validate_only=True)
        assert prepared.creation_validation is not None
        committed = note.note(
            vault,
            **arguments,
            draft_id=prepared.draft_id,
            draft_token=prepared.draft_token,
            draft_hash=prepared.draft_hash,
            relation_disposition="reviewed_none",
            relation_review_hash=prepared.draft_hash,
            relation_review_reason="No honest typed relation is established by this fixture input.",
        )
    stored = (vault / committed.path).read_text(encoding="utf-8")
    metadata = provenance.parse_origin(stored, managed=True)
    assert metadata.status == "valid"
    document = semantic_index.current_parent_index_state(vault, committed.path).document
    assert (
        provenance.match_origin_scope(
            metadata.payload["bindings"][0]["scope"], document=document
        ).status
        == "found"
    )


def test_unrelated_crlf_append_preserves_existing_multiline_origin_metadata(vault: Path) -> None:
    """Raw CRLF carry-through must not become new attribution after LF semantic parsing."""
    from exomem import append_to_file, note

    binding = _write(vault, "Original evidence.\n")
    payload = {"inputs": {"original": binding}, "assessments": [], "bindings": []}
    canonical = provenance.encode_origin(payload)
    multiline = "<!-- exomem-origin:v1\n" + json.dumps(payload, indent=2) + "\n-->"
    with request_scope(RequestPrincipal(audience_id="client-a")):
        created = note.note(
            vault,
            content=canonical + "\n\n## Findings\n\n- [finding] Existing claim.\n",
            note_type="insight",
            title="Platform newline workflow",
            status="draft",
        )
        page = vault / created.path
        raw = page.read_text(encoding="utf-8").replace(canonical, multiline).replace("\n", "\r\n")
        page.write_bytes(raw.encode("utf-8"))
        _write(vault, "Later evidence.\n")
        appended = append_to_file.append_to_file(
            vault,
            path=created.path,
            content="\r\n## Later details\r\n\r\nUnrelated appendix.\r\n",
            allow_curated=True,
        )
    assert appended.path == created.path
    stored = page.read_bytes().decode("utf-8")
    assert multiline.replace("\n", "\r\n") in stored
    assert "Unrelated appendix." in stored


def _parent_with_origin(vault: Path, *, configured: bool) -> tuple[object, str]:
    from test_episode_recovery import _write_source_rule

    from exomem import note

    binding = _write(vault, "Original evidence.\n")
    if configured:
        _write_source_rule(vault, ceiling=6)
    rationale = "Retained assessment rationale only for an authorized reader."
    block = provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [
                {
                    "inputs": ["original"],
                    "basis": "agent_assessment",
                    "by": "fixture-agent",
                    "reason": rationale,
                }
            ],
            "bindings": [],
        }
    )
    with request_scope(RequestPrincipal(audience_id="client-a")):
        created = note.note(
            vault,
            content=block + "\n\n## Observations\n\n- [finding] Public claim ^claim\n",
            note_type="insight",
            title="Released parent with private provenance",
            status="draft",
        )
    return created, rationale


def _hand_edit_above_carrier(parent: Path, prefix: str) -> None:
    text = parent.read_text(encoding="utf-8")
    at = text.index("<!-- exomem-origin")
    parent.write_text(text[:at] + prefix + text[at:], encoding="utf-8")


@pytest.mark.parametrize(
    "input_change, prefix",
    [
        ("withheld", ""),
        ("deleted", ""),
        # A literal `<!--` cannot fold the reserved opener into an ordinary comment.
        ("withheld", "HTML comments open with <!-- in markup.\n"),
        # Nor can indentation turn the carrier into code that is shown as written.
        ("withheld", "Para.\n\n    "),
    ],
    ids=["withheld", "deleted", "stray-opener", "indented-into-code"],
)
def test_public_parent_read_removes_the_whole_unreleased_origin_payload(
    vault: Path, input_change: str, prefix: str
) -> None:
    """An unreleased or unavailable input hides its whole carrier, never the released claim."""
    from test_episode_recovery import _write_source_rule

    from exomem import commands

    created, rationale = _parent_with_origin(vault, configured=True)
    parent = vault / created.path
    _hand_edit_above_carrier(parent, prefix)
    canonical = parent.read_bytes()
    if input_change == "withheld":
        _write_source_rule(vault, ceiling=0)
    else:
        (vault / _PATH).unlink()
    with request_scope(RequestPrincipal(audience_id="client-a")):
        projected = commands.op_get(vault, path=created.path, include_raw=True)
        metadata_only = commands.op_get(
            vault, path=created.path, frontmatter_only=True, include_raw=True
        )
        unit = commands.op_read_memory(vault, path=created.path, unit_ref=created.ref + "#claim")
    wire = json.dumps(projected, default=str)
    assert rationale not in wire and "exomem-origin" not in wire and "fixture-agent" not in wire
    assert "Public claim" in projected["body"]
    assert "content" not in projected
    assert rationale not in json.dumps(metadata_only, default=str)
    assert "content" not in metadata_only
    unit_wire = json.dumps(unit, default=str)
    assert "Public claim" in unit_wire and rationale not in unit_wire
    assert parent.read_bytes() == canonical


def test_an_unclosed_fence_above_an_unreleased_carrier_never_discloses_it(vault: Path) -> None:
    """Code context decides what a writer may author, never what a restricted reader sees."""
    from test_episode_recovery import _write_source_rule

    from exomem import commands

    created, rationale = _parent_with_origin(vault, configured=True)
    _hand_edit_above_carrier(vault / created.path, "```\n")
    _write_source_rule(vault, ceiling=0)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        projected = commands.op_get(vault, path=created.path)
    wire = json.dumps(projected, default=str)
    assert rationale not in wire and "exomem-origin" not in wire
    assert "Public claim" in projected["body"]


@pytest.mark.parametrize("input_change", ["stale", "unconfigured"])
def test_stale_or_unconfigured_origin_stays_readable_as_written(
    vault: Path, input_change: str
) -> None:
    """Staleness is accounting state; with no file policy, nothing is projected at all."""
    from exomem import commands

    created, rationale = _parent_with_origin(vault, configured=input_change == "stale")
    _write(vault, "Changed original evidence.\n")
    with request_scope(RequestPrincipal(audience_id="client-a")):
        page = commands.op_get(vault, path=created.path, include_raw=True)
        unit = commands.op_read_memory(vault, path=created.path, unit_ref=created.ref + "#claim")
    assert rationale in page["body"] and rationale in page["content"]
    assert "Public claim" in json.dumps(unit, default=str)


@pytest.mark.parametrize(
    "prefix", ["", "```\n", "Para.\n\n    "], ids=["prose", "unclosed-fence", "indented-into-code"]
)
def test_search_never_matches_text_that_lives_only_in_an_origin_carrier(
    vault: Path, prefix: str
) -> None:
    """A carrier is attribution, not prose: matching it would make search an oracle for it.

    Every audience searches the same fields, so a hand edit that pushes the
    carrier into code must not turn its payload into searchable text.
    """
    from exomem import commands

    created, rationale = _parent_with_origin(vault, configured=True)
    _hand_edit_above_carrier(vault / created.path, prefix)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        found = commands.op_find(
            vault, query="rationale authorized reader fixture-agent", mode="keyword"
        )
        claim = commands.op_find(vault, query="Public claim", mode="keyword")
    assert created.path not in json.dumps(found, default=str)
    claim_wire = json.dumps(claim, default=str)
    assert created.path in claim_wire
    assert rationale not in claim_wire and "fixture-agent" not in claim_wire


def test_a_restricted_pack_never_ledes_with_a_carrier_pushed_into_code(vault: Path) -> None:
    """A packed page's lede is prose for every audience, never an indented carrier."""
    from exomem import commands

    created, rationale = _parent_with_origin(vault, configured=True)
    _hand_edit_above_carrier(vault / created.path, "    ")
    with request_scope(RequestPrincipal(audience_id="client-a")):
        packed = commands.op_find(vault, query="Public claim", mode="keyword", pack=True)
    assert created.path in packed["pack"]["packed_paths"]
    wire = json.dumps(packed, default=str)
    assert rationale not in wire and "fixture-agent" not in wire


def test_the_owner_reads_a_fenced_carrier_example_as_written_in_a_configured_vault(
    vault: Path,
) -> None:
    """Nothing is withheld from the owner, so a documented example stays literal."""
    from test_episode_recovery import _write_source_rule

    from exomem import commands, note

    _write_source_rule(vault, ceiling=6)
    example = "```\n<!-- exomem-origin:v1 {\"example\":true} -->\n```\n"
    with request_scope(owner_principal(surface="mcp")):
        created = note.note(
            vault,
            content="How a carrier looks:\n\n" + example,
            note_type="insight",
            title="Documented carrier example",
            status="draft",
        )
        read = commands.op_get(vault, path=created.path, include_raw=True)
    assert example in read["body"]
    assert read["content"] == (vault / created.path).read_text(encoding="utf-8")


def test_an_excerpt_never_starts_with_released_origin_metadata(vault: Path) -> None:
    """An excerpt-level reader gets prose, even when the carrier's inputs are released."""
    from exomem import commands
    from exomem.governance import membership, policy

    created, rationale = _parent_with_origin(vault, configured=True)
    root = vault / "Knowledge Base" / "_Governance"
    parent = created.path.removeprefix("Knowledge Base/")
    (root / "scopes" / "parent.yaml").write_text(
        f'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAC\nname: Parent\npaths: ["{parent}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "parent-client-a.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAD\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAC"]\naudience: client-a\nceiling: 5\n',
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    membership.clear_memo()
    policy._CACHE.clear()
    with request_scope(RequestPrincipal(audience_id="client-a")):
        excerpt = commands.op_get(vault, path=created.path)
    assert excerpt["release_level"] == egress.LEVEL_EXCERPT
    assert "exomem-origin" not in excerpt["body"] and rationale not in excerpt["body"]
    assert excerpt["body"].strip()


def test_unassessed_parent_payload_is_hidden_but_raw_capture_is_uninterpreted(vault: Path) -> None:
    """Legacy unsupported attribution cannot leak; the same captured bytes remain evidence."""
    from test_episode_recovery import _write_source_rule

    from exomem import commands

    _write_source_rule(vault, ceiling=6)
    carrier = "<!-- exomem-origin:v2 unavailable attribution details -->"
    body = f"{carrier}\n\nPublic prose. <!-- ordinary comment --> Sample `<!-- exomem-origin:v2 example -->`.\n"
    parent = vault / "Knowledge Base/Notes/Insights/legacy.md"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(f"---\ntype: insight\n---\n\n{body}", encoding="utf-8")
    canonical = parent.read_bytes()
    _write(vault, body)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        projected = commands.op_get(vault, path=parent.relative_to(vault).as_posix(), include_raw=True)
        raw_capture = commands.op_get(vault, path=_PATH, include_raw=True)
    assert "unavailable attribution details" not in json.dumps(projected, default=str)
    assert "content" not in projected
    # An unreleased page withholds every reserved opener, an inline-code example too.
    assert "Sample ``." in projected["body"] and "<!-- ordinary comment -->" in projected["body"]
    assert carrier in raw_capture["body"] and carrier in raw_capture["content"]
    assert parent.read_bytes() == canonical


@pytest.mark.parametrize("change", ["bytes", "permission"])
def test_public_origin_read_checks_earlier_inputs_after_later_resolution(
    vault: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A later input read cannot preserve attribution after an earlier input changes."""
    from test_episode_recovery import _write_source_rule

    from exomem import commands, note

    first = _write(vault, "First original evidence.\n")
    second = _write(
        vault,
        "Second original evidence.\n",
        relative="Knowledge Base/Sources/second.md",
        identity="12345678-1234-5678-1234-567812345679",
    )
    _write_source_rule(vault, ceiling=6)
    reason = "Assessment whose inputs must both remain available."
    carrier = provenance.encode_origin(
        {
            "inputs": {"first": first, "second": second},
            "assessments": [
                {
                    "inputs": ["first", "second"],
                    "basis": "agent_assessment",
                    "by": "fixture-agent",
                    "reason": reason,
                }
            ],
            "bindings": [],
        }
    )
    resolve = origin_bindings.resolve_origin_input
    changed = False

    def change_earlier_input(
        root: Path, binding: object, **kwargs: object
    ) -> origin_bindings.OriginInputProof:
        nonlocal changed
        proof = resolve(root, binding, **kwargs)
        if proof.retained.canonical == second["reference"] and not changed:
            changed = True
            if change == "bytes":
                _write(root, "Replacement evidence.\n")
            else:
                _write_source_rule(root, ceiling=0)
        return proof

    with request_scope(RequestPrincipal(audience_id="client-a")):
        created = note.note(
            vault,
            content=carrier + "\n\nPublic claim.\n",
            note_type="insight",
            title="Two retained inputs",
            status="draft",
        )
        parent = vault / created.path
        canonical = parent.read_bytes()
        assert reason in commands.op_get(vault, path=created.path)["body"]
        monkeypatch.setattr(origin_bindings, "resolve_origin_input", change_earlier_input)
        result = commands.op_get(vault, path=created.path, include_raw=True)
    wire = json.dumps(result, default=str)
    assert changed and reason not in wire and "exomem-origin" not in wire
    assert "Public claim." in result["body"] and "content" not in result
    assert parent.read_bytes() == canonical


def test_retained_input_with_nonempty_policy_reads_a_unique_unicode_leaf(vault: Path) -> None:
    """Retained proofs must not fail on an ordinary decomposed physical Source name."""
    import unicodedata

    from test_episode_recovery import _write_source_rule

    logical = "Knowledge Base/Sources/café.md"
    physical = unicodedata.normalize("NFD", logical)
    binding = _write(vault, "Exact retained evidence.\n", relative=physical)
    _write_source_rule(vault, ceiling=6)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        proof = origin_bindings.resolve_origin_input(vault, binding)
    assert proof.root == _REF
    assert proof.retained.page.path == physical and proof.text == "Exact retained evidence.\n"
    for guard in proof.guards:
        guard.recheck(vault)


def _carrier_with_reason(vault: Path, reason: str) -> str:
    binding = _write(vault, "Original evidence.\n")
    return provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [
                {"inputs": ["original"], "basis": "agent_assessment", "by": "agent", "reason": reason}
            ],
            "bindings": [],
        }
    )


def test_link_normalization_leaves_a_carrier_reason_byte_identical(vault: Path) -> None:
    """A carrier is recorded data: the writer normalizes the prose link, never the reason's."""
    from exomem import note

    reason = "Raised in the [[Linked page]] review."
    block = _carrier_with_reason(vault, reason)
    with request_scope(RequestPrincipal(audience_id="client-a")):
        note.note(vault, content="Linked claim.\n", note_type="insight", title="Linked page", status="draft")
        created = note.note(
            vault,
            content=block + "\n\nSee [[Linked page]].\n",
            note_type="insight",
            title="Parent page",
            status="draft",
        )

    written = (vault / created.path).read_text(encoding="utf-8")
    assert provenance.parse_origin(written, managed=True).payload["assessments"][0]["reason"] == reason
    assert "See [[Knowledge Base/Notes/Insights/linked-page]]." in written


def test_a_move_rewrites_the_prose_link_but_never_the_carrier(vault: Path) -> None:
    """Moving a page updates links to it in prose; a carrier keeps the bytes it recorded."""
    from exomem import move_file

    target = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"
    block = _carrier_with_reason(vault, f"Raised in [[{target}]].")
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "carrier-referrer.md"
    referrer.write_text(
        "---\ntype: insight\nstatus: draft\ncreated: 2026-10-01\nupdated: 2026-10-01\n---\n\n"
        f"{block}\n\nSee [[{target}]].\n",
        encoding="utf-8",
    )

    move_file.move_file(
        vault, old_path=target + ".md", new_path="Knowledge Base/Notes/Insights/renamed-disclosure.md"
    )

    text = referrer.read_text(encoding="utf-8")
    assert block in text
    assert "See [[Knowledge Base/Notes/Insights/renamed-disclosure]]." in text


_DOC_TARGET = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"


def _doc_note(vault: Path) -> Path:
    """A page documenting the carrier format: an opener in inline code, no carrier."""
    from exomem import find as find_module

    doc = vault / "Knowledge Base" / "Notes" / "Insights" / "carrier-format-notes.md"
    doc.write_text(
        "---\ntype: insight\nstatus: draft\ncreated: 2026-10-01\nupdated: 2026-10-01\n---\n\n"
        "# Carrier format notes\n\nA carrier starts with `<!-- exomem-origin` and stays hidden.\n\n"
        f"See [[{_DOC_TARGET}]] for the disclosure rule.\n\nThe quokkafact lives in this later paragraph.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return doc


def test_the_owner_finds_prose_after_a_documented_opener(vault: Path) -> None:
    from exomem import commands

    doc = _doc_note(vault)

    with request_scope(owner_principal(surface="mcp")):
        found = commands.op_find(vault, query="quokkafact", mode="keyword")

    assert doc.name in json.dumps(found, default=str)


def test_a_link_after_a_documented_opener_stays_inbound(vault: Path) -> None:
    from exomem import commands

    _doc_note(vault)

    with request_scope(owner_principal(surface="mcp")):
        read = commands.op_read_memory(vault, path=_DOC_TARGET + ".md", links=True)

    assert "carrier-format-notes" in json.dumps(read["links"]["inbound"], default=str)


def test_a_move_retargets_a_link_after_a_documented_opener(vault: Path) -> None:
    from exomem import move_file

    doc = _doc_note(vault)

    move_file.move_file(
        vault, old_path=_DOC_TARGET + ".md", new_path="Knowledge Base/Notes/Insights/renamed-disclosure.md"
    )

    assert "See [[Knowledge Base/Notes/Insights/renamed-disclosure]] for" in doc.read_text(encoding="utf-8")
