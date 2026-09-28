"""Japanese readiness, end to end (close-memory-loop task 6.3).

A Japanese user's own vault is mostly Japanese, written without spaces between
words. These fixtures build one from the invented pages of the Japanese recall
vault (`epistemic.corpora.recall_japanese_vault`) plus a few anchors of their
own, and drive the real product paths over it: activation, the referential
cues, learning from a correction, Records claims routing, structure promotion
and the vocabulary fold. A second group pins the cross-lingual case (a
Japanese turn about English notes) and the negative twins.

Every name is invented.
"""

from __future__ import annotations

import random
import re
import string
import time
import uuid
from pathlib import Path

import pytest
from epistemic.corpora.recall_japanese_vault import (
    PARTICLE_QUERIES,
    TARGET_ENTITIES,
    TARGET_NOTES,
    background_notes,
)
from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_hot_projection import _edit, _live, _one_old_tick

from exomem import (
    activation_conventions,
    capture_sweep,
    collection_claims,
    commands,
    due_state,
    lexstore,
    semantic_writes,
    structure_promotion,
    working_set_heat,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
    writer_lease,
)
from exomem.governance.principal import request_scope
from exomem.vault import content_hash
from exomem.vocabulary_fold import fold_term

KB = "Knowledge Base"
FALCON = f"{KB}/Products/ハヤブサ号.md"
AOKI = f"{KB}/Entities/People/青木陽介.md"
EXOMEM = f"{KB}/Systems/Exomem.md"
WAGON = f"{KB}/Products/Harlow Wagon.md"
SECRET = f"{KB}/Products/月影プロジェクト.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(title: str, summary: str, *, kind: str = "note", extra: str = "") -> str:
    return (
        f"---\ntype: {kind}\nstatus: active\nupdated: 2026-09-02\n{extra}---\n\n"
        f"# {title}\n\n## Summary\n\n{summary}\n"
    )


def _collection(exomem_id: str, title: str, terms: list[str]) -> str:
    return f"""---
type: collection
exomem_id: {exomem_id}
title: {title}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
claims:
  terms: [{", ".join(terms)}]
item_schema:
  natural_key: [observed_on]
  fields:
    observed_on:
      type: date
      required: true
    state:
      type: string
---

{title}
"""


def _seed_japanese(vault: Path) -> None:
    kb = vault / KB
    _write(
        kb / "Products" / "ハヤブサ号.md",
        _page(
            "ハヤブサ号",
            "家族で使っている青いワゴン車。油圧センサーは去年の車検で交換した。",
        ),
    )
    entity = TARGET_ENTITIES[0]
    _write(
        kb / "Entities" / "People" / f"{entity.name}.md",
        "---\ntype: entity\nentity_type: person\nstatus: active\n---\n\n"
        f"# {entity.name}\n\n## Summary\n\n{entity.summary}\n",
    )
    _write(
        kb / "Systems" / "Exomem.md",
        _page("Exomem", "個人の知識ベース。検索のレイテンシは一秒以内が目標。"),
    )
    _write(
        kb / "Products" / "Harlow Wagon.md",
        _page(
            "Harlow Wagon",
            "The family wagon. The oil pressure sensor was replaced at the last inspection.",
        ),
    )
    _write(
        kb / "Products" / "月影プロジェクト.md",
        _page("月影プロジェクト", "非公開の計画。"),
    )
    for index, note in enumerate((*TARGET_NOTES, *background_notes())):
        _write(
            kb / "Notes" / "Findings" / f"ja-{index:03d}-{note.key}.md",
            _page(note.title, note.observation, kind="finding"),
        )
    _write(
        kb / "Records" / "設備の不具合" / "_collection.md",
        _collection("5a0e1c2d-3b4f-4a6d-8e9f-0a1b2c3d4e51", "設備の不具合", ["故障", "修理", "空調", "水漏れ"]),
    )
    (kb / "Records" / "設備の不具合" / "Items").mkdir(parents=True, exist_ok=True)
    _write(
        kb / "Records" / "体重の記録" / "_collection.md",
        _collection("5a0e1c2d-3b4f-4a6d-8e9f-0a1b2c3d4e52", "体重の記録", ["体重", "健康", "測定"]),
    )
    (kb / "Records" / "体重の記録" / "Items").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def ja_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_japanese(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    _one_old_tick(vault)
    _live(vault)
    lexstore.ensure_fresh(vault)
    due_state.reconcile(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


@pytest.fixture(autouse=True)
def _fresh_registry():
    activation_conventions.clear_cache()
    yield
    activation_conventions.clear_cache()


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _learn(vault: Path, rel: str, names: list[str]) -> None:
    text = (vault / rel).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=rel,
        why="the user calls it this",
        operation={
            "kind": "patch_frontmatter",
            "field": "learned_aliases",
            "value": names,
            "expected_hash": content_hash(text),
        },
    )
    working_set_index.WorkingSetIndex(vault).update()
    working_set_runtime.reset_caches_for_tests()


def _fresh_session() -> None:
    """A new conversation: no continuity token, no in-process packet cache."""
    working_set_runtime.reset_caches_for_tests()
    activation_conventions.clear_cache()


def _statuses(packet: dict) -> dict[str, str]:
    return {item["path"]: item["status"] for item in packet["anchors"]}


def _resolved(packet: dict) -> list[str]:
    return [item["path"] for item in packet["anchors"] if item["status"] == "resolved"]


def _activate(vault: Path, turn: str, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, **kwargs)


# --------------------------------------------------------------------------- #
# Japanese on Japanese
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("turn", "path"),
    [
        ("ハヤブサ号の油圧センサーは旅行前に交換すべき？", FALCON),
        ("ハヤブサ号について教えて", FALCON),
        ("青木陽介の仕事は何だっけ", AOKI),
    ],
    ids=["product-in-a-sentence", "product-and-a-particle", "person"],
)
def test_a_japanese_name_inside_an_unsegmented_sentence_resolves(
    ja_vault: Path, turn: str, path: str
) -> None:
    """Japanese marks a word's edge with a particle, not a space: a name
    between two particles is spelled as a word, like a spaced token."""
    packet = _activate(ja_vault, turn)
    assert _resolved(packet) == [path], (packet.get("abstention"), packet["anchors"])
    assert "exact_alias" in packet["anchors"][0]["evidence"]


def test_a_japanese_name_inside_a_longer_compound_is_never_resolved(ja_vault: Path) -> None:
    """The twin: ハヤブサ号線 (a railway line) holds the car's name inside a
    longer compound. That is containment, the weak `rare_term`, never a name."""
    packet = _activate(ja_vault, "ハヤブサ号線の工事はいつ終わる？")
    assert FALCON not in _resolved(packet)
    assert _statuses(packet).get(FALCON) in (None, "partial")


def test_the_words_a_japanese_run_holds_are_read_between_its_particles() -> None:
    analysis = working_set_resolve.analyze_turn("ハヤブサ号の油圧センサーは旅行前に交換すべき？")
    assert analysis.words == ("ハヤブサ号", "油圧センサー", "旅行前", "交換すべき")
    # One Han character is as often a verb stem as a word: never a word here.
    assert working_set_resolve.analyze_turn("隼を借りた").words == ()
    # Hiragana alone, Chinese without a Latin word, and Latin are untouched.
    assert working_set_resolve.analyze_turn("これはなんですか").words == ()
    assert working_set_resolve.analyze_turn("设备故障").words == ()
    assert working_set_resolve.analyze_turn("the cargo sled").words == ()
    assert working_set_resolve.analyze_turn("Exomemのレイテンシ").words == ("exomem", "レイテンシ")


@pytest.mark.parametrize("turn", ["続けて", "どこまでやった？", "続けてください", "前回の続きをお願い"])
def test_a_japanese_pointing_back_turn_carries_recent_context_like_continue(
    ja_vault: Path, turn: str
) -> None:
    token = _activate(ja_vault, "ハヤブサ号の油圧センサーは旅行前に交換すべき？")["continuity"]
    english = _activate(ja_vault, "continue", continuity=token)
    japanese = _activate(ja_vault, turn, continuity=token)
    assert _resolved(english) == [FALCON], (english.get("abstention"), english["anchors"])
    assert _resolved(japanese) == _resolved(english), (
        japanese.get("abstention"),
        japanese["anchors"],
    )


@pytest.mark.parametrize(
    ("turn", "referential"),
    [
        ("続けて", True),
        ("続けてください", True),
        ("どこまでやったっけ？", True),
        ("続きは？", True),
        ("続きを読んで", False),
        ("宿題はどこまでやった？", False),
        ("これから", False),
    ],
)
def test_a_japanese_cue_turn_is_referential_only_with_nothing_else_said(
    turn: str, referential: bool
) -> None:
    assert working_set_resolve.analyze_turn(turn).referential is referential


def test_a_japanese_pointing_back_turn_without_a_token_resumes_the_freshest_edit(
    ja_vault: Path,
) -> None:
    _edit(ja_vault, FALCON, "青いワゴン車", "紺色のワゴン車")
    english = _activate(ja_vault, "continue", session=f"fresh-{uuid.uuid4().hex}")
    packet = _activate(ja_vault, "続けて", session=f"fresh-{uuid.uuid4().hex}")
    assert _resolved(english) == [FALCON], (english.get("abstention"), english["anchors"])
    assert _resolved(packet) == [FALCON], (packet.get("abstention"), packet["anchors"])


def test_a_learned_japanese_alias_works_in_a_fresh_session(ja_vault: Path) -> None:
    turn = "隼号の車検はいつ？"
    assert FALCON not in _statuses(_activate(ja_vault, turn))

    _learn(ja_vault, FALCON, ["隼号"])
    _fresh_session()

    packet = _activate(ja_vault, turn)
    assert _resolved(packet) == [FALCON], (packet.get("abstention"), packet["anchors"])


def test_a_learned_japanese_cue_works_in_a_fresh_session(ja_vault: Path) -> None:
    token = _activate(ja_vault, "ハヤブサ号の油圧センサーは旅行前に交換すべき？")["continuity"]
    assert FALCON not in _resolved(_activate(ja_vault, "例の件をお願い", continuity=token))
    current = activation_conventions.load_conventions(ja_vault)
    saved = commands.op_schema_memory(
        ja_vault,
        operation="save-conventions",
        subject="activation-conventions",
        proposal={"schema_version": 1, "referential": {"add_cues": ["例の件"]}},
        why="the user resumes in Japanese",
        expected_hash=current.content_hash,
    )
    assert saved["saved"] is not None
    _fresh_session()

    packet = _activate(ja_vault, "例の件をお願い", continuity=token)
    assert _resolved(packet) == [FALCON], (packet.get("abstention"), packet["anchors"])


def test_a_mixed_script_turn_resolves_by_its_latin_name(ja_vault: Path) -> None:
    packet = _activate(ja_vault, "Exomemのレイテンシ")
    assert _resolved(packet) == [EXOMEM], (packet.get("abstention"), packet["anchors"])


# --------------------------------------------------------------------------- #
# Claims routing, structure promotion, the fold
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "terms",
    [["エアコンの故障と水漏れ", "修理"], ["空調機の故障"], ["故障", "水漏れ"]],
    ids=["title-and-tag", "compound", "tags"],
)
def test_a_japanese_incident_page_routes_to_its_records_collection(
    ja_vault: Path, terms: list[str]
) -> None:
    routing = semantic_writes._records_routing_from_terms(ja_vault, terms, {"type": ["note"]})
    assert routing is not None
    assert routing["title"] == "設備の不具合", routing


def test_a_japanese_page_routes_by_its_own_subject_or_not_at_all(ja_vault: Path) -> None:
    weight = semantic_writes._records_routing_from_terms(ja_vault, ["朝の体重測定", "健康"], {})
    assert weight is not None and weight["title"] == "体重の記録", weight
    # One shared claim word is coincidence, in Japanese as in English.
    assert semantic_writes._records_routing_from_terms(ja_vault, ["故障した時計"], {}) is None
    assert semantic_writes._records_routing_from_terms(ja_vault, ["茶道教室の予定"], {}) is None


def test_japanese_claims_terms_survive_normalisation() -> None:
    assert collection_claims.normalize_terms(["故障", "空調", "水漏れ"]) == frozenset(
        {"故障", "空調", "水漏"}
    )


def test_structure_promotion_terms_keep_japanese_words() -> None:
    terms = structure_promotion._terms(["故障", "エアコンの故障", "体重測定", "ねこ", "Exomemのレイテンシ"])
    assert terms == frozenset({"故障", "エアコン", "体重測定", "ねこ", "exomem", "レイテンシ"})
    # A particle, one Han character or one kana is never a term.
    assert structure_promotion._terms(["の", "車", "は"]) == frozenset()


def test_ascii_terms_split_exactly_as_the_old_regex_did() -> None:
    old = re.compile(r"[^a-z0-9]+")
    rng = random.Random(63)
    alphabet = string.ascii_letters + string.digits + " -_/.,'"
    samples = ["Depot stock", "a-b", "Q3 2026 plan", "the oil pressure", "re_use it"]
    samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))) for _ in range(500)]
    for value in samples:
        expected = frozenset(
            token
            for token in old.split(value.casefold())
            if len(token) > 2
            and token not in structure_promotion._STOPWORDS
            and not token.isdigit()
        )
        assert structure_promotion._terms([value]) == expected, value


def test_width_and_kana_variants_fold_sensibly() -> None:
    assert collection_claims.normalize_terms(["ｴｱｺﾝ"]) == frozenset({"エアコン"})
    assert collection_claims.normalize_terms(["ＥＸＯＭＥＭ"]) == frozenset({"exomem"})
    assert fold_term("ｴｱｺﾝ") == fold_term("エアコン")
    assert fold_term("か\u3099") == fold_term("が")
    # Hiragana and katakana spell different words; the fold never merges them.
    assert fold_term("えあこん") != fold_term("エアコン")


# --------------------------------------------------------------------------- #
# Cross-lingual: a Japanese turn about English notes
# --------------------------------------------------------------------------- #


def test_cross_lingual_today_abstains_then_one_learned_alias_reaches_the_page(
    ja_vault: Path,
) -> None:
    """No word of the turn is on the English page. Without a learned name the
    turn abstains (dense evidence may only order or propose); one correction
    teaches the Japanese name and the same turn resolves."""
    turn = "ハーロウの油圧センサーは旅行前に交換すべき？"
    today = _activate(ja_vault, turn)
    assert WAGON not in _statuses(today)
    assert today["abstained"] is True

    _learn(ja_vault, WAGON, ["ハーロウ"])
    _fresh_session()
    packet = _activate(ja_vault, turn)
    assert _resolved(packet) == [WAGON], (packet.get("abstention"), packet["anchors"])


def test_cross_lingual_one_correction_offers_the_japanese_name_and_teaches_it(
    ja_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole loop, as an agent runs it: the turn misses, the agent picks
    the English page, the pick's advisory offers the words of the Japanese
    sentence (not the sentence as one word), and the name it teaches reaches
    the page in a fresh session."""
    monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: True)
    turn = "ハーロウの油圧センサーは旅行前に交換すべき？"
    assert _resolved(_activate(ja_vault, turn, session="session-one")) == []

    picked = _activate(ja_vault, turn, anchor=WAGON, session="session-one")
    advisory = picked["learning"]
    assert advisory["turn_terms"][:2] == ["ハーロウ", "油圧センサー"], advisory
    name = next(option for option in advisory["options"] if option["family"] == "name")
    writer_lease.invoke_command(
        _command("edit_memory"),
        ja_vault,
        path=name["path"],
        why="the user calls the wagon ハーロウ",
        operation={
            "kind": "patch_frontmatter",
            "field": name["field"],
            "value": [*name["current"], advisory["turn_terms"][0]],
            "expected_hash": name["expected_hash"],
        },
    )
    working_set_index.WorkingSetIndex(ja_vault).update()

    _fresh_session()
    packet = _activate(ja_vault, turn, session="session-two")
    assert _resolved(packet) == [WAGON], (packet.get("abstention"), packet["anchors"])
    # The twin keeps the sentence and changes the subject: still nothing.
    other = _activate(ja_vault, "自転車の油圧センサーは旅行前に交換すべき？", session="session-two")
    assert WAGON not in _resolved(other)


def test_cross_lingual_latin_name_in_a_japanese_sentence_is_a_lead_not_a_decision(
    ja_vault: Path,
) -> None:
    """`Harlow` is one word of the two-word title: `rare_term`, which needs a
    second contact the English page cannot give a Japanese sentence."""
    packet = _activate(ja_vault, "Harlowの油圧センサーは旅行前に交換すべき？")
    assert _statuses(packet).get(WAGON) == "partial", packet["anchors"]


# --------------------------------------------------------------------------- #
# Capture-time bilingual aliases (the owner's ruling: option C)
# --------------------------------------------------------------------------- #

CORVANE = f"{KB}/Entities/Organizations/Corvane Motors.md"
TESSARY = f"{KB}/Entities/Organizations/Tessary Works.md"


def _create_entity(vault: Path, name: str, summary: str, **kwargs) -> str:
    result = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="organization",
        name=name,
        summary=summary,
        **kwargs,
    )
    working_set_index.WorkingSetIndex(vault).update()
    _fresh_session()
    return result["path"]


def test_a_japanese_turn_reaches_an_english_entity_captured_with_a_japanese_alias(
    ja_vault: Path,
) -> None:
    """The agent writes the English page and, in the same capture, the name the
    user says in Japanese. The Japanese turn resolves the page by that alias;
    its twin, captured without one, stays out of reach."""
    corvane = _create_entity(
        ja_vault,
        "Corvane Motors",
        "The carmaker that built the family wagon.",
        aliases=["コルヴェイン"],
    )
    tessary = _create_entity(ja_vault, "Tessary Works", "The tool shop by the station.")
    assert (corvane, tessary) == (CORVANE, TESSARY)

    packet = _activate(ja_vault, "コルヴェインの保証はいつまで？")
    assert _resolved(packet) == [CORVANE], (packet.get("abstention"), packet["anchors"])
    assert "exact_alias" in packet["anchors"][0]["evidence"]

    twin = _activate(ja_vault, "テッサリーの工具はどこ？")
    assert TESSARY not in _resolved(twin), twin["anchors"]
    assert TESSARY not in _statuses(twin)


def test_a_japanese_alias_added_by_edit_reaches_the_english_entity(ja_vault: Path) -> None:
    """The same alias, added later through the owner's alias field."""
    _create_entity(ja_vault, "Tessary Works", "The tool shop by the station.")
    turn = "テッサリーの工具はどこ？"
    assert TESSARY not in _statuses(_activate(ja_vault, turn))

    text = (ja_vault / TESSARY).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        _command("edit_memory"),
        ja_vault,
        path=TESSARY,
        why="the user writes the shop's name in katakana",
        operation={
            "kind": "patch_frontmatter",
            "field": "aliases",
            "value": ["テッサリー"],
            "expected_hash": content_hash(text),
        },
    )
    working_set_index.WorkingSetIndex(ja_vault).update()
    _fresh_session()

    packet = _activate(ja_vault, turn)
    assert _resolved(packet) == [TESSARY], (packet.get("abstention"), packet["anchors"])


def test_a_japanese_alias_inside_a_longer_compound_is_never_resolved(ja_vault: Path) -> None:
    """The alias is a name like any other: inside a longer compound it is only
    contained, and a contained name never resolves on its own."""
    _create_entity(
        ja_vault, "Corvane Motors", "The carmaker.", aliases=["コルヴェイン"]
    )
    packet = _activate(ja_vault, "コルヴェインスキーの小説を読んだ")
    assert CORVANE not in _resolved(packet), packet["anchors"]


# --------------------------------------------------------------------------- #
# The alias guard answers for every name the resolver matches (review round)
# --------------------------------------------------------------------------- #


def _refused(vault: Path, name: str, aliases: list[str]) -> str:
    with pytest.raises(ValueError, match="ENTITY_EXISTS") as info:
        commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="organization",
            name=name,
            summary="Refused.",
            aliases=aliases,
        )
    assert not (vault / KB / "Entities" / "Organizations" / f"{name}.md").exists()
    return str(info.value)


def test_an_alias_a_note_already_answers_to_is_refused(ja_vault: Path) -> None:
    """ハヤブサ号 is a note, not an entity. As another page's alias it would
    make the note's own name resolve both pages."""
    _refused(ja_vault, "Corvane Motors", ["ハヤブサ号"])
    working_set_index.WorkingSetIndex(ja_vault).update()
    _fresh_session()
    packet = _activate(ja_vault, "ハヤブサ号の油圧センサーは旅行前に交換すべき？")
    assert _resolved(packet) == [FALCON], packet["anchors"]


def test_an_alias_spelled_with_a_typographic_apostrophe_is_refused(ja_vault: Path) -> None:
    """The index reads U+2019 as the plain apostrophe, so `Dana’s Garage` is
    the existing entity's own name."""
    _create_entity(ja_vault, "Dana's Garage", "The garage on the corner.")
    _refused(ja_vault, "Corvane Motors", ["Dana\u2019s Garage"])


def test_an_alias_with_a_soft_hyphen_is_refused(ja_vault: Path) -> None:
    """A soft hyphen is dropped by the index: テッ\u00adサリー is テッサリー."""
    _create_entity(ja_vault, "Tessary Works", "The tool shop.", aliases=["テッサリー"])
    _refused(ja_vault, "Corvane Motors", ["テッ\u00adサリー"])


def test_an_alias_only_a_withheld_page_answers_to_reads_as_absent(ja_vault: Path) -> None:
    """The guard decides visibility first: a caller who may not see the page
    gets the same answer as if it did not exist, and never its path."""
    write_scope(ja_vault, paths=SECRET, name="Hidden")
    write_rule(ja_vault, ceiling=0)
    _reset_caches()
    with request_scope(_external()):
        result = commands.op_connect_memory(
            ja_vault,
            operation="create-entity",
            entity_type="organization",
            name="Corvane Motors",
            summary="A carmaker.",
            aliases=["月影プロジェクト"],
        )
    assert result["path"] == CORVANE
    assert "月影" not in str(result.get("warnings"))


def test_one_create_walks_the_entities_once_however_many_aliases(
    ja_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each walk of Entities/ parses 青木陽介's page once, so its count is the
    number of walks: the same for one alias as for eight."""
    from exomem import entity_candidates

    walks: list[int] = []
    original = entity_candidates.parse_frontmatter

    def counting(source: str):
        if "# 青木陽介" in source:
            walks.append(1)
        return original(source)

    monkeypatch.setattr(entity_candidates, "parse_frontmatter", counting)
    _create_entity(ja_vault, "Corvane Motors", "One alias.", aliases=["コルヴェイン"])
    one = len(walks)
    walks.clear()
    _create_entity(
        ja_vault, "Tessary Works", "Eight aliases.", aliases=[f"テッサリー{n}" for n in range(8)]
    )
    assert len(walks) == one <= 2, (one, len(walks))


def test_edit_memory_refuses_an_alias_another_page_answers_to(ja_vault: Path) -> None:
    """The second route the guidance names runs the same guard."""
    _create_entity(ja_vault, "Tessary Works", "The tool shop by the station.")
    text = (ja_vault / TESSARY).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="ENTITY_EXISTS"):
        writer_lease.invoke_command(
            _command("edit_memory"),
            ja_vault,
            path=TESSARY,
            why="a wrong alias",
            operation={
                "kind": "patch_frontmatter",
                "field": "aliases",
                "value": ["テッサリー", "青木陽介"],
                "expected_hash": content_hash(text),
            },
        )
    assert "青木陽介" not in (ja_vault / TESSARY).read_text(encoding="utf-8")
    working_set_index.WorkingSetIndex(ja_vault).update()
    _fresh_session()
    assert _resolved(_activate(ja_vault, "青木陽介の仕事は何だっけ")) == [AOKI]


def test_edit_memory_keeps_a_page_s_own_names_when_it_rewrites_its_aliases(
    ja_vault: Path,
) -> None:
    """The page's own title and current aliases are not a collision."""
    _create_entity(ja_vault, "Tessary Works", "The tool shop.", aliases=["テッサリー"])
    text = (ja_vault / TESSARY).read_text(encoding="utf-8")
    writer_lease.invoke_command(
        _command("edit_memory"),
        ja_vault,
        path=TESSARY,
        why="the user also writes it in hiragana",
        operation={
            "kind": "patch_frontmatter",
            "field": "aliases",
            "value": ["テッサリー", "Tessary Works", "てっさりー"],
            "expected_hash": content_hash(text),
        },
    )
    assert "てっさりー" in (ja_vault / TESSARY).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# A name that begins in hiragana keeps its own edge (review round)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("turn", "path"),
    [
        ("ハヤブサ号はどう？", FALCON),
        ("ハヤブサ号はもう車検に出した？", FALCON),
        ("青木陽介はいつ来る？", AOKI),
        ("ハヤブサ号をまた洗車した", FALCON),
        ("ハヤブサ号がまだ故障中", FALCON),
    ],
)
def test_a_name_followed_by_a_particle_glued_to_more_kana_resolves(
    ja_vault: Path, turn: str, path: str
) -> None:
    """The most common phrasing: a particle ends the name even when the next
    kana word is written straight after it (はどう, をまた, がまだ)."""
    packet = _activate(ja_vault, turn)
    assert _resolved(packet) == [path], (packet.get("abstention"), packet["anchors"])
    assert "exact_alias" in packet["anchors"][0]["evidence"]


@pytest.mark.parametrize(
    "turn", ["ねこやなぎ銀行の口座を解約したい", "駅前のねこやなぎ銀行で口座を作った"]
)
def test_a_hiragana_name_after_a_particle_run_stays_one_name(turn: str) -> None:
    """The control: ねこやなぎ does not start with a particle, and after の the
    rest of the run joins the kanji that follows, so the bank stays one word."""
    words = working_set_resolve.analyze_turn(turn).words
    assert "ねこやなぎ銀行" in words
    assert "銀行" not in words


def test_a_name_that_starts_with_hiragana_is_not_handed_to_its_kanji_tail(
    ja_vault: Path,
) -> None:
    """ねこやなぎ is part of the bank's name, not a particle: only a hiragana
    run that is wholly a declared particle or filler word is a word edge."""
    _write(ja_vault / KB / "Products" / "銀行.md", _page("銀行", "銀行の手続き全般のメモ。"))
    _create_entity(ja_vault, "ねこやなぎ銀行", "The bank the family uses.")
    turn = "ねこやなぎ銀行の口座を解約したい"
    assert "銀行" not in working_set_resolve.analyze_turn(turn).words
    packet = _activate(ja_vault, turn)
    bank_note = f"{KB}/Products/銀行.md"
    assert bank_note not in _resolved(packet), packet["anchors"]
    assert _resolved(packet) == [f"{KB}/Entities/Organizations/ねこやなぎ銀行.md"], packet["anchors"]


# --------------------------------------------------------------------------- #
# Negative twins and the budget
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("turn", [*PARTICLE_QUERIES, "の", "こと", "これ", "はい", "それで"])
def test_particles_and_short_kana_never_resolve(ja_vault: Path, turn: str) -> None:
    packet = _activate(ja_vault, turn)
    assert _resolved(packet) == [], packet["anchors"]
    assert working_set_resolve.analyze_turn(turn).referential is False


def test_a_withheld_japanese_page_never_leaks(ja_vault: Path) -> None:
    owner = _activate(ja_vault, "月影プロジェクトの進み具合はどう？")
    assert _resolved(owner) == [SECRET]
    write_scope(ja_vault, paths=SECRET, name="Hidden")
    write_rule(ja_vault, ceiling=0)
    _reset_caches()
    with request_scope(_external()):
        packet = _activate(ja_vault, "月影プロジェクトの進み具合はどう？")
    assert SECRET not in str(packet)
    assert "月影" not in str(packet)


def test_japanese_activation_stays_inside_the_latency_budget(ja_vault: Path) -> None:
    turns = [
        "ハヤブサ号の油圧センサーは旅行前に交換すべき？",
        "青木陽介の仕事は何だっけ",
        "続けてください",
        "Exomemのレイテンシ",
        "茶道教室で抹茶を用意するのは誰ですか",
        "来月の合宿、山小屋をまた借りられるか確認して、駅前の自転車店でパンク修理も頼んでおいてくれる？",
    ]
    for turn in turns:
        _activate(ja_vault, turn)
    working_set_runtime.reset_caches_for_tests()
    worst = 0.0
    for turn in turns:
        started = time.perf_counter()
        _activate(ja_vault, turn)
        worst = max(worst, time.perf_counter() - started)
    assert worst < 1.0, worst
