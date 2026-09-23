"""The census's optional judged sample: refs only, seeded, folded with Wilson."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import epistemic_graph, relation_census
from exomem.__main__ import main

NOTES = "Knowledge Base/Notes"
SOURCE = "Knowledge Base/Sources/Articles/source-one"


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _sample_vault(vault: Path) -> Path:
    """Specific edges in four families, plus generic and link edges to skip."""
    _write(
        vault,
        f"{SOURCE}.md",
        "---\ntype: source\ncaptured: 2026-01-01\n---\n# Source one\n\nText.\n",
    )
    for index in range(8):
        lines = [
            f"- supports [[{NOTES}/hub-{index % 3}]]",
            f"- relates_to [[{NOTES}/hub-{(index + 1) % 3}]]",
            f"- links_to [[{NOTES}/hub-{(index + 2) % 3}]]",
        ]
        if index % 2 == 0:
            lines.append(f"- depends_on [[{NOTES}/hub-{index % 3}]]")
        if index % 4 == 0:
            lines.append(f"- cites [[{SOURCE}]]")
        if index == 5:
            lines.append(f"- evidenced_by [[{SOURCE}]]")
        _write(
            vault,
            f"{NOTES}/note-{index}.md",
            f"---\ntype: insight\n---\n# Kestrel Note {index}\n\n" + "\n".join(lines) + "\n",
        )
    for index in range(3):
        _write(vault, f"{NOTES}/hub-{index}.md", f"---\ntype: insight\n---\n# Hub {index}\n\nx\n")
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault


def test_sample_is_seeded_and_stratified_by_family(tmp_path: Path) -> None:
    vault = _sample_vault(tmp_path / "vault")

    first = relation_census.sample(vault, size=6, seed=7)
    again = relation_census.sample(vault, size=6, seed=7)

    assert json.dumps(first) == json.dumps(again)
    # 8 supports, 4 depends_on, 2 cites and 1 evidenced_by: specific authored
    # edges only, never relates_to or links_to.
    assert first["strata"] == {"citation": 2, "dependency": 4, "evidence": 1, "support": 8}
    assert first["drawn"] == 6
    drawn_families = [item["family"] for item in first["items"]]
    assert set(drawn_families) == {"citation", "dependency", "evidence", "support"}
    assert drawn_families.count("support") >= drawn_families.count("dependency")
    for item in first["items"]:
        assert set(item) == {"id", "family", "relation", "source", "target", "verdict"}
        assert set(item["source"]) == {"path", "anchor"}
        assert set(item["target"]) == {"path", "anchor"}
        assert item["relation"] not in {"relates_to", "links_to"}
        assert item["verdict"] is None
    assert "Kestrel" not in json.dumps(first)
    assert first["verdicts"] == [
        "precise",
        "too_specific",
        "wrong_direction",
        "wrong_predicate",
        "should_be_generic",
    ]

    everything = relation_census.sample(vault, size=100, seed=7)
    assert everything["drawn"] == 15
    other_seed = relation_census.sample(vault, size=6, seed=8)
    assert other_seed["strata"] == first["strata"]
    assert other_seed["drawn"] == 6


def _judged(verdicts: list[str | None]) -> dict:
    return {
        "kind": "relation_census_sample",
        "items": [
            {
                "id": f"s{index:03d}",
                "family": "support",
                "relation": "supports",
                "source": {"path": "p", "anchor": None},
                "target": {"path": "q", "anchor": None},
                "verdict": verdict,
            }
            for index, verdict in enumerate(verdicts)
        ],
    }


def test_judged_file_folds_with_a_wilson_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    judged = _judged(["precise"] * 7 + ["wrong_predicate", "wrong_predicate", "too_specific", None])

    folded = relation_census.fold_judgments(judged)

    assert folded["judged"] == 10
    assert folded["unjudged"] == 1
    assert folded["false"] == 3
    assert folded["rate"] == pytest.approx(0.3)
    assert folded["wilson_95"] == [
        pytest.approx(0.1078, abs=1e-4),
        pytest.approx(0.6032, abs=1e-4),
    ]
    assert folded["by_verdict"] == {
        "precise": 7,
        "too_specific": 1,
        "wrong_direction": 0,
        "wrong_predicate": 2,
        "should_be_generic": 0,
    }
    with pytest.raises(ValueError, match="INVALID_JUDGMENT"):
        relation_census.fold_judgments(_judged(["plausible"]))

    vault = _sample_vault(tmp_path / "vault")
    judged_path = tmp_path / "judged.json"
    judged_path.write_text(json.dumps(judged), encoding="utf-8")
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    exit_code = main(
        ["relations", "census", "--json", "--vault", str(vault), "--judged", str(judged_path)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["metrics"]["false_precision_judged"]["false"] == 3


def test_absent_judgments_report_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _sample_vault(tmp_path / "vault")

    assert relation_census.census(vault)["metrics"]["false_precision_judged"] == "unmeasured"
    assert relation_census.fold_judgments(_judged([None, None])) == "unmeasured"

    sample_path = tmp_path / "sample.json"
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    exit_code = main(
        [
            "relations",
            "census",
            "--json",
            "--vault",
            str(vault),
            "--sample",
            "5",
            "--sample-out",
            str(sample_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    written = json.loads(sample_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["sample"] == {
        "requested": 5,
        "drawn": 5,
        "strata": written["strata"],
    }
    assert payload["metrics"]["false_precision_judged"] == "unmeasured"
    assert "Knowledge Base" not in json.dumps(payload)
    assert all(item["verdict"] is None for item in written["items"])


def test_sample_out_defaults_to_the_vault_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exomem import state_paths

    vault = _sample_vault(tmp_path / "vault")
    working = tmp_path / "working-directory"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)

    exit_code = main(["relations", "census", "--json", "--vault", str(vault), "--sample", "4"])

    captured = capsys.readouterr()
    written = state_paths.vault_state_dir(vault) / "relation-census" / "sample.json"
    assert exit_code == 0
    assert list(working.iterdir()) == []
    assert json.loads(written.read_text(encoding="utf-8"))["drawn"] == 4
    assert str(written) in captured.err
    assert json.loads(captured.out)["sample"]["drawn"] == 4
