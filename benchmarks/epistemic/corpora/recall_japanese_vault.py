"""A Japanese-only vault for lexical recall acceptance
(``recall-japanese-vault-v1``).

About eighty invented Japanese pages -- twenty-one target notes and entities
that the queries in :data:`QUERIES` ask for, one Records collection, one
Planning item and generated office-routine notes that share the everyday
vocabulary (particles, weekdays, rooms) a real Japanese vault repeats on every
page. Nothing in it is English, so tokenizer v1 read none of it.

Every page goes through the product writers (compiled note, entity, Records,
Planning) in an isolated child with private state, as the context-activation
corpus does, so the lexical catalogue, the activation index and any embedding
sidecar built over it are the real ones. Every name, place and title is
invented.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from epistemic.corpora.context_activation import (
    FixtureError,
    _compiled_note,
    _create_planning_item,
    _create_records_collection,
    _entity,
    _planning_manifest,
    _records_manifest,
)

CORPUS_ID = "recall-japanese-vault-v1"
BACKGROUND_SEED = 20260923
BACKGROUND_COUNT = 58


@dataclass(frozen=True)
class TargetNote:
    key: str
    title: str
    observation: str


@dataclass(frozen=True)
class TargetEntity:
    key: str
    name: str
    summary: str


@dataclass(frozen=True)
class Query:
    query: str
    gold: str


TARGET_NOTES: tuple[TargetNote, ...] = (
    TargetNote("tea-ceremony", "茶道教室の予定",
               "茶道教室は第二土曜日に開かれます。抹茶と和菓子は講師が用意します。"),
    TargetNote("cat-vaccine", "猫の予防接種",
               "飼い猫のミケは毎年十月に動物病院で予防接種を受けます。"),
    TargetNote("bread", "天然酵母パンの作り方",
               "天然酵母のパンは一晩発酵させてから二百度で焼きます。"),
    TargetNote("passport", "パスポートの更新",
               "パスポートの有効期限は来年三月です。更新には写真と戸籍謄本が必要です。"),
    TargetNote("piano", "ピアノの発表会",
               "ピアノの発表会は十二月に市民ホールで開かれます。曲はワルツです。"),
    TargetNote("car-insurance", "自動車保険の更新",
               "自動車保険は四月に更新します。今年は車両保険を外しました。"),
    TargetNote("dentist", "歯医者の予約",
               "歯医者の予約は毎月第三水曜日の午後四時です。"),
    TargetNote("hiking", "霧ヶ原山の登山記録",
               "霧ヶ原山には稲荷口から登り、山頂まで二時間かかりました。"),
    TargetNote("tax-return", "確定申告の準備",
               "確定申告の書類は二月末までに税理士へ送ります。医療費の領収書も添付します。"),
    TargetNote("tomato", "ベランダ菜園のミニトマト",
               "ベランダのミニトマトは七月に収穫できました。支柱が必要です。"),
    TargetNote("router", "自宅の回線とルーター",
               "ルーターのパスワードは本棚の裏のメモに書いてあります。回線は光回線です。"),
    TargetNote("recycling", "資源ごみの日",
               "資源ごみは毎週木曜日の朝八時までに出します。瓶と缶は分けます。"),
    TargetNote("book-club", "読書会の課題本",
               "来月の読書会では長編小説「青い灯台」を読みます。感想は一人三分です。"),
    TargetNote("marathon", "マラソン大会の練習",
               "秋のマラソン大会に向けて、週に三回十キロ走ります。"),
    TargetNote("movers", "引っ越し業者の見積もり",
               "引っ越し業者の見積もりは三社から取りました。一番安いのは十二万円でした。"),
    TargetNote("english-lesson", "英会話のレッスン",
               "オンライン英会話のレッスンは火曜と金曜の夜九時からです。"),
    TargetNote("bicycle-repair", "自転車のパンク修理",
               "自転車の後輪がパンクしたので、駅前の自転車店で修理してもらいました。"),
    TargetNote("concert", "コンサートのチケット",
               "十一月の弦楽四重奏コンサートのチケットを二枚購入しました。"),
    TargetNote("minutes", "定例会の記録",
               "議事録は翌日までに共有します。決定事項は赤字で記載します。"),
)

TARGET_ENTITIES: tuple[TargetEntity, ...] = (
    TargetEntity("friend-aoki", "青木陽介",
                 "大学時代の友人で、今は北の町で建築設計の仕事をしている。"),
    TargetEntity("teacher-hayase", "早瀬美月",
                 "料理教室の先生。毎月、季節の和食を教えている。"),
)

QUERIES: tuple[Query, ...] = (
    Query("茶道教室はいつ", "tea-ceremony"),
    Query("ミケの予防接種", "cat-vaccine"),
    Query("天然酵母パンの焼き方", "bread"),
    Query("パスポート更新 写真", "passport"),
    Query("ピアノ発表会の会場", "piano"),
    Query("自動車保険の更新時期", "car-insurance"),
    Query("歯医者の予約", "dentist"),
    Query("霧ヶ原山の登山", "hiking"),
    Query("確定申告 医療費 領収書", "tax-return"),
    Query("ミニトマトの収穫", "tomato"),
    Query("ルーターのパスワード", "router"),
    Query("資源ごみの曜日", "recycling"),
    Query("読書会の課題本", "book-club"),
    Query("マラソン大会の練習", "marathon"),
    Query("引っ越し業者の見積もり", "movers"),
    Query("英会話のレッスン時間", "english-lesson"),
    Query("後輪のパンク修理", "bicycle-repair"),
    Query("弦楽四重奏のチケット", "concert"),
    Query("青木陽介の仕事", "friend-aoki"),
    Query("早瀬美月 料理教室", "teacher-hayase"),
    # "X の Y" and question forms: the particles are not on the page.
    Query("抹茶の用意", "tea-ceremony"),
    Query("茶道教室で抹茶を用意するのは誰ですか", "tea-ceremony"),
    Query("会議の議事録はいつ共有", "minutes"),
    # Question words built on 何 ask rather than name.
    Query("パンは何度で焼きますか", "bread"),
    Query("山は何時間かかりましたか", "hiking"),
    Query("自動車保険は何月に更新しますか", "car-insurance"),
    Query("茶道教室は何曜日ですか", "tea-ceremony"),
)

#: Questions made only of particles and function words: no page is about them.
PARTICLE_QUERIES: tuple[str, ...] = ("についてですか", "はいつですか", "のためにします", "それはなんですか")

#: Generated background: one routine per (room, task) pair.
_ROOMS = ("会議室", "倉庫", "駐車場", "受付", "食堂", "図書室", "研修室", "屋上", "玄関", "休憩室")
_TASKS = ("清掃", "点検", "予約", "片付け", "掲示", "消毒", "換気", "照明の交換", "鍵の管理", "備品の補充")
_WHEN = ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "毎朝", "毎晩", "月末")
_TEAMS = ("総務", "経理", "営業", "開発", "広報")


def background_notes(
    seed: int = BACKGROUND_SEED, count: int = BACKGROUND_COUNT
) -> tuple[TargetNote, ...]:
    """Deterministic office-routine notes that share the targets' particles."""
    rng = random.Random(seed)
    pairs = [(room, task) for room in _ROOMS for task in _TASKS]
    rng.shuffle(pairs)
    notes = []
    for index, (room, task) in enumerate(pairs[:count]):
        when, team = rng.choice(_WHEN), rng.choice(_TEAMS)
        notes.append(
            TargetNote(
                f"routine-{index:02d}",
                f"{room}の{task}",
                f"{room}の{task}は{when}に行います。担当は{team}です。",
            )
        )
    return tuple(notes)


RECORDS_MANIFEST = "Knowledge Base/Records/Body Weight/_collection.md"
PLANNING_MANIFEST = "Knowledge Base/Planning/Family Trip/_collection.md"


def corpus_digest() -> str:
    payload = {
        "notes": [[n.key, n.title, n.observation] for n in (*TARGET_NOTES, *background_notes())],
        "entities": [[e.key, e.name, e.summary] for e in TARGET_ENTITIES],
        "queries": [[q.query, q.gold] for q in QUERIES],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _build_in_process(root: Path) -> dict[str, str]:
    """Render the vault under ``root``; return logical key -> vault path."""
    from exomem.init import init_vault

    root = Path(root)
    init_vault(root)
    key_to_path: dict[str, str] = {}
    for index, note in enumerate((*TARGET_NOTES, *background_notes())):
        key_to_path[note.key] = _compiled_note(
            root,
            title=note.title,
            slug=f"ja-{index:03d}-{note.key}",
            observation=note.observation,
            category="finding",
        )
    for index, entity in enumerate(TARGET_ENTITIES):
        key_to_path[entity.key] = _entity(
            root, name=entity.name, slug=f"ja-person-{index}", summary=entity.summary
        )
    key_to_path["records-weight"] = _create_records_collection(
        root,
        manifest_path=RECORDS_MANIFEST,
        manifest_text=_records_manifest(
            exomem_id="70000000-0000-4000-8000-000000000001",
            title="体重の記録",
            source="Items",
            claims=("体重", "健康"),
            natural_key="observed_on",
            fields=(
                "    observed_on:\n"
                "      type: date\n"
                "      required: true\n"
                "    kilograms:\n"
                "      type: string\n"
                "      required: true\n"
                "    note:\n"
                "      type: string"
            ),
        ),
        items=(
            ("70000000-0000-4000-8000-000000000011",
             {"observed_on": "2026-08-01", "kilograms": "64.2", "note": "朝食前に測定"}),
            ("70000000-0000-4000-8000-000000000012",
             {"observed_on": "2026-09-01", "kilograms": "63.5", "note": "散歩を続けた"}),
        ),
    )
    key_to_path["planning-trip"] = _create_planning_item(
        root,
        manifest_path=PLANNING_MANIFEST,
        manifest_text=_planning_manifest(
            exomem_id="80000000-0000-4000-8000-000000000001",
            title="家族旅行",
        ),
        plan_id="80000000-0000-4000-8000-000000000011",
        title="南の島への家族旅行",
    )
    return key_to_path


def _run_isolated_build_request(request_path: str, result_path: str) -> None:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    key_to_path = _build_in_process(Path(request["root"]))
    Path(result_path).write_text(
        json.dumps(key_to_path, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def build_corpus(root: Path) -> dict[str, str]:
    """Build the vault in a child with private state; return key -> vault path.

    The caller's environment and Exomem singletons are never rebound, and any
    writer refusal fails the build rather than falling back to hand-written
    bytes.
    """
    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="exomem-recall-japanese-") as runtime_raw:
        runtime = Path(runtime_raw)
        paths = {
            name: runtime / name
            for name in ("state", "xdg-state", "logs", "call-ledger", "writer-lease", "tmp")
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        request = runtime / "request.json"
        result = runtime / "result.json"
        request.write_text(json.dumps({"root": str(root)}), encoding="utf-8")
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_")
        }
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    part
                    for part in (
                        str(repository / "src"),
                        str(repository / "benchmarks"),
                        environment.get("PYTHONPATH", ""),
                    )
                    if part
                ),
                "EXOMEM_STATE_ROOT": str(paths["state"]),
                "EXOMEM_CONFIG_PATH": str(runtime / "config.json"),
                "EXOMEM_LOG_DIR": str(paths["logs"]),
                "EXOMEM_CALL_LEDGER_DIR": str(paths["call-ledger"]),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(paths["writer-lease"]),
                "EXOMEM_LEASE_COORDINATOR_DB": str(runtime / "lease-coordinator.sqlite"),
                "EXOMEM_VAULT_PATH": str(root),
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_GRAPH_DRAIN": "1",
                "EXOMEM_DISABLE_GRAPH_SCHEDULING": "1",
                "XDG_STATE_HOME": str(paths["xdg-state"]),
                "TMPDIR": str(paths["tmp"]),
            }
        )
        code = (
            "from epistemic.corpora.recall_japanese_vault import _run_isolated_build_request; "
            f"_run_isolated_build_request({str(request)!r}, {str(result)!r})"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise FixtureError("isolated Japanese vault build exceeded 300 seconds") from error
        if completed.returncode != 0:
            detail = completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()
            raise FixtureError(f"isolated Japanese vault build failed: {detail}")
        return json.loads(result.read_text(encoding="utf-8"))
