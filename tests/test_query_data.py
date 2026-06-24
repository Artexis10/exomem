"""query_data: structured queries over CSV/JSON data files under the vault."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_mcp import query_data as qd


CSV = (
    "date,analyte,value,unit,ref_range\n"
    "2016-03,IGF-1,276,ng/ml,111 - 551\n"
    "2024-07,IGF-1,77,ng/ml,90 - 357\n"
    '2024-07,CRP,"<0,4",mg/l,<5\n'
    "2025-05,CRP,0.42,mg/l,<5\n"
    "2020-09,Hb,151,g/l,134 - 170\n"
)


def _write(vault: Path, rel: str, text: str) -> str:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


# ---------------- CSV ----------------

def test_csv_loads_rows_and_columns(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(vault, path=rel)
    assert r.format == "csv"
    assert r.total_rows == 5
    assert r.columns == ["date", "analyte", "value", "unit", "ref_range"]
    assert r.returned == 5


def test_csv_filter_eq(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(vault, path=rel, filters=[{"column": "analyte", "op": "eq", "value": "IGF-1"}])
    assert r.total_matched == 2
    assert {row["value"] for row in r.rows} == {"276", "77"}


def test_csv_numeric_gt_with_comma_and_lab_operator(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    # CRP "<0,4" -> 0.4 ; 0.42 -> 0.42. value < 0.41 keeps only the "<0,4" row.
    r = qd.query_data(vault, path=rel, filters=[
        {"column": "analyte", "op": "eq", "value": "CRP"},
        {"column": "value", "op": "lt", "value": 0.41},
    ])
    assert r.total_matched == 1
    assert r.rows[0]["value"] == "<0,4"


def test_csv_numeric_gt(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(vault, path=rel, filters=[
        {"column": "analyte", "op": "eq", "value": "IGF-1"},
        {"column": "value", "op": "gt", "value": 100},
    ])
    assert r.total_matched == 1 and r.rows[0]["value"] == "276"


def test_csv_date_range(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(vault, path=rel, date_from="2024-01", date_to="2024-12")
    assert r.total_matched == 2
    assert {row["date"] for row in r.rows} == {"2024-07"}


def test_csv_columns_projection(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(vault, path=rel, columns=["date", "value"], limit=2)
    assert r.columns == ["date", "value"]
    assert all(set(row.keys()) == {"date", "value"} for row in r.rows)


def test_csv_sort_numeric_descending(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(
        vault, path=rel,
        filters=[{"column": "analyte", "op": "eq", "value": "IGF-1"}],
        sort_by="value", descending=True,
    )
    assert [row["value"] for row in r.rows] == ["276", "77"]


def test_csv_limit_offset_truncation(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    r = qd.query_data(vault, path=rel, limit=2, offset=0)
    assert r.returned == 2 and r.total_matched == 5 and r.truncated is True


def test_csv_aggregate_count_max_latest_distinct(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    assert qd.query_data(vault, path=rel, aggregate="count").aggregate == {"count": 5}

    igf = [{"column": "analyte", "op": "eq", "value": "IGF-1"}]
    assert qd.query_data(vault, path=rel, filters=igf, aggregate="max:value").aggregate["max"] == 276.0
    latest = qd.query_data(vault, path=rel, filters=igf, aggregate="latest:value").aggregate
    assert latest["row"]["value"] == "77"  # 2024-07 is later than 2016-03

    distinct = qd.query_data(vault, path=rel, aggregate="distinct:analyte").aggregate
    assert distinct["n"] == 3 and set(distinct["distinct"]) == {"IGF-1", "CRP", "Hb"}


# ---------------- JSON ----------------

def test_json_top_level_array(vault: Path) -> None:
    data = [{"date": "2024-07-26", "analyte": "IGF-1", "value": 77},
            {"date": "2025-05-21", "analyte": "B12", "value": 392}]
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.json", json.dumps(data))
    r = qd.query_data(vault, path=rel, filters=[{"column": "analyte", "op": "eq", "value": "IGF-1"}])
    assert r.format == "json" and r.total_rows == 2 and r.total_matched == 1
    assert r.rows[0]["value"] == 77


def test_json_nested_record_path_and_dotted_column(vault: Path) -> None:
    data = {"sections": {"log": [
        {"performer": {"name": "Confido"}, "dt": "2024"},
        {"performer": {"name": "Hugo"}, "dt": "2025"},
    ]}}
    rel = _write(vault, "Knowledge Base/Evidence/Test/log.json", json.dumps(data))
    r = qd.query_data(
        vault, path=rel, record_path="sections.log",
        filters=[{"column": "performer.name", "op": "eq", "value": "Confido"}],
        columns=["performer.name", "dt"],
    )
    assert r.total_matched == 1
    assert r.rows[0] == {"performer.name": "Confido", "dt": "2024"}


def test_json_common_key_autodetect(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/r.json", json.dumps({"result": [{"a": 1}, {"a": 2}]}))
    r = qd.query_data(vault, path=rel)
    assert r.total_rows == 2
    assert any("auto-detected" in w for w in r.warnings)


def test_json_bad_record_path(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/r.json", json.dumps({"result": [{"a": 1}]}))
    with pytest.raises(qd.QueryDataError) as e:
        qd.query_data(vault, path=rel, record_path="nope.missing")
    assert e.value.code == "BAD_RECORD_PATH"


# ---------------- errors / safety ----------------

def test_not_found(vault: Path) -> None:
    with pytest.raises(qd.QueryDataError) as e:
        qd.query_data(vault, path="Knowledge Base/Evidence/Test/missing.csv")
    assert e.value.code == "NOT_FOUND"


def test_path_escape_rejected(vault: Path) -> None:
    with pytest.raises(qd.QueryDataError) as e:
        qd.query_data(vault, path="../../../etc/passwd")
    assert e.value.code == "INVALID_PATH"


def test_unsupported_format(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/x.md", "# not data\n")
    with pytest.raises(qd.QueryDataError) as e:
        qd.query_data(vault, path=rel)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_bad_op(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Evidence/Test/labs.csv", CSV)
    with pytest.raises(qd.QueryDataError) as e:
        qd.query_data(vault, path=rel, filters=[{"column": "value", "op": "between", "value": 1}])
    assert e.value.code == "BAD_OP"


def test_numeric_filter_ignores_appended_units_and_dates(vault: Path) -> None:
    # Older rows store value+unit together ("100,2 nmol/l"); a `< 50` filter must
    # parse the leading number, not lexicographically string-compare.
    csv = (
        "date,analyte,value\n"
        '2016-03,VitD,"22,8 nmol/l"\n'
        '2016-11,VitD,"100,2 nmol/l"\n'
        "2024-04,VitD,21.1\n"
    )
    rel = _write(vault, "Knowledge Base/Evidence/Test/vitd.csv", csv)
    r = qd.query_data(vault, path=rel, filters=[{"column": "value", "op": "lt", "value": 50}])
    assert {row["value"] for row in r.rows} == {"22,8 nmol/l", "21.1"}  # 100,2 excluded


# ---------------- profile + dataset card ("what it holds") ----------------

FINANCE_CSV = (
    "date,vendor,item,amount\n"
    "2025-08-25,Ugreen,Nexode 100W charger,28.56\n"
    "2025-08-25,Ugreen,USB4 240W cable,12.59\n"
    "2025-07-01,DJI,Mavic battery,99.00\n"
)


def test_profile_data_summarizes_columns(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Finance/invoices.csv", FINANCE_CSV)
    prof = qd.profile_data(vault, path=rel)
    assert prof["total_rows"] == 3
    cols = {c["name"]: c for c in prof["columns"]}
    assert cols["amount"]["kind"] == "numeric"
    assert cols["amount"]["sum"] == pytest.approx(140.15)
    assert cols["amount"]["min"] == pytest.approx(12.59)
    assert cols["amount"]["max"] == pytest.approx(99.00)
    assert cols["vendor"]["kind"] == "categorical"
    assert set(cols["vendor"]["top_values"]) == {"Ugreen", "DJI"}
    assert cols["date"]["kind"] == "date"
    assert cols["date"]["earliest"] == "2025-07-01"
    assert cols["date"]["latest"] == "2025-08-25"


def test_build_dataset_card_carries_frontmatter_and_content(vault: Path) -> None:
    rel = _write(vault, "Knowledge Base/Finance/invoices.csv", FINANCE_CSV)
    prof = qd.profile_data(vault, path=rel)
    card = qd.build_dataset_card(prof, title="Invoice register")
    assert "type: dataset" in card
    assert f"data_file: {rel}" in card
    # Salient content the card must expose so `find` can hit it semantically:
    assert "Ugreen" in card            # a vendor (categorical top value)
    assert "Nexode 100W charger" in card  # an item value
    assert "2025-07-01" in card        # date range floor
    # A prose placeholder for Claude's "what this holds" summary:
    assert "what this holds" in card.lower()


def test_query_data_profile_aggregate_returns_card(vault: Path) -> None:
    # `aggregate="profile"` is how Claude gets a server-side profile + a ready
    # dataset card for a raw file it can't read directly.
    rel = _write(vault, "Knowledge Base/Finance/invoices.csv", FINANCE_CSV)
    r = qd.query_data(vault, path=rel, aggregate="profile")
    assert r.aggregate["profile"]["total_rows"] == 3
    card = r.aggregate["dataset_card"]
    assert "type: dataset" in card
    assert f"data_file: {rel}" in card
    assert "Ugreen" in card


def test_raw_data_files_are_never_embeddable() -> None:
    # The never-embed-rows invariant: CSV/JSON are NOT part of the embedding
    # corpus (only the markdown dataset card is). Locks Hugo's noise concern.
    from kb_mcp import embeddings
    assert embeddings._is_embeddable_path(Path("Knowledge Base/Finance/x.csv")) is False
    assert embeddings._is_embeddable_path(Path("Knowledge Base/Finance/x.json")) is False
    assert embeddings._is_embeddable_path(Path("Knowledge Base/Finance/x.md")) is True
