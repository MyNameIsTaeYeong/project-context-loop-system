"""인간 앵커 입출력 + generator 정밀도 테스트 (PR #79 P5)."""

from __future__ import annotations

import csv
from pathlib import Path

from context_loop.eval.gold_set import (
    GoldItem,
    GoldSet,
    GraphEntityRef,
    SupportingFact,
)
from context_loop.eval.human_anchor import (
    HUMAN_VERDICT_COLUMNS,
    REVIEW_CSV_COLUMNS,
    ReviewVerdict,
    export_review_csv,
    generator_precision,
    import_review_csv,
    parse_verdict_cell,
    serves_unit,
)


def _sg_item(item_id: str, **kw) -> GoldItem:  # type: ignore[no-untyped-def]
    base = dict(
        id=item_id,
        query="결제 인증 서비스가 의존하는 검증 모듈은?",
        relevant_doc_ids=[12],
        reference_answer="Token Validator에 의존한다.",
        measurement_units=["doc", "answer", "graph"],
        supporting_facts=[
            SupportingFact(
                entity="Auth Service", relation="depends_on",
                target="Token Validator",
                evidence_span="Auth Service가 Token Validator에 의존한다.",
                source_doc_id=12,
            ),
        ],
        relevant_graph_entities=[GraphEntityRef(name="Auth Service", type="system")],
        source_type="confluence_mcp",
    )
    base.update(kw)
    return GoldItem(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_verdict_cell / serves_unit
# ---------------------------------------------------------------------------


def test_parse_verdict_cell_tristate() -> None:
    assert parse_verdict_cell("1") is True
    assert parse_verdict_cell("yes") is True
    assert parse_verdict_cell("O") is True
    assert parse_verdict_cell("0") is False
    assert parse_verdict_cell("no") is False
    assert parse_verdict_cell("") is None
    assert parse_verdict_cell(None) is None
    assert parse_verdict_cell("아마도") is None


def test_serves_unit_explicit_and_inferred() -> None:
    explicit = _sg_item("q1")
    assert serves_unit(explicit, "graph") is True
    assert serves_unit(explicit, "answer") is True
    legacy = GoldItem(id="q2", query="?", relevant_doc_ids=[1])
    assert serves_unit(legacy, "doc") is True
    assert serves_unit(legacy, "graph") is False


# ---------------------------------------------------------------------------
# export / import round-trip
# ---------------------------------------------------------------------------


def test_export_review_csv_columns_and_empty_verdicts(tmp_path: Path) -> None:
    gold = GoldSet(items=[_sg_item("q1")])
    path = tmp_path / "review.csv"
    n = export_review_csv(gold, path)
    assert n == 1
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == list(REVIEW_CSV_COLUMNS)
    # 컨텍스트 채워짐
    assert rows[0]["query"].startswith("결제 인증")
    assert rows[0]["measurement_units"] == "doc,answer,graph"
    assert "Auth Service" in rows[0]["evidence_spans"]
    assert "depends_on" in rows[0]["supporting_facts"]
    # verdict 컬럼은 비어 있음
    for col in HUMAN_VERDICT_COLUMNS:
        assert rows[0][col] == ""


def test_export_then_import_blank_yields_none(tmp_path: Path) -> None:
    gold = GoldSet(items=[_sg_item("q1")])
    path = tmp_path / "review.csv"
    export_review_csv(gold, path)
    verdicts = import_review_csv(path)
    assert verdicts["q1"].valid is None
    assert verdicts["q1"].doc_valid is None


def test_export_sample_n_stratified_deterministic(tmp_path: Path) -> None:
    items = (
        [_sg_item(f"c{i}", source_type="confluence_mcp") for i in range(5)]
        + [_sg_item(f"g{i}", source_type="git_code") for i in range(5)]
    )
    gold = GoldSet(items=items)
    p1 = tmp_path / "a.csv"
    p2 = tmp_path / "b.csv"
    n1 = export_review_csv(gold, p1, sample_n=4, seed=7)
    export_review_csv(gold, p2, sample_n=4, seed=7)
    assert n1 == 4
    # 같은 seed → 같은 표본 (결정론)
    assert p1.read_text() == p2.read_text()
    # 층화 — 두 source_type 모두 포함
    with open(p1, encoding="utf-8") as f:
        types = {r["source_type"] for r in csv.DictReader(f)}
    assert types == {"confluence_mcp", "git_code"}


def test_export_sample_n_none_exports_all(tmp_path: Path) -> None:
    gold = GoldSet(items=[_sg_item(f"q{i}") for i in range(3)])
    path = tmp_path / "all.csv"
    assert export_review_csv(gold, path, sample_n=None) == 3


# ---------------------------------------------------------------------------
# import_review_csv
# ---------------------------------------------------------------------------


def test_import_review_csv_parses_filled(tmp_path: Path) -> None:
    path = tmp_path / "filled.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(REVIEW_CSV_COLUMNS))
        w.writeheader()
        w.writerow({
            "id": "q1", "human_valid": "1", "human_doc_valid": "1",
            "human_answer_valid": "0", "human_graph_valid": "",
            "human_notes": "답이 약간 부정확",
        })
    verdicts = import_review_csv(path)
    v = verdicts["q1"]
    assert v.valid is True
    assert v.doc_valid is True
    assert v.answer_valid is False
    assert v.graph_valid is None
    assert v.notes == "답이 약간 부정확"


# ---------------------------------------------------------------------------
# generator_precision
# ---------------------------------------------------------------------------


def test_generator_precision_overall_and_by_unit() -> None:
    gold = GoldSet(items=[_sg_item("q1"), _sg_item("q2"), _sg_item("q3")])
    verdicts = {
        "q1": ReviewVerdict(id="q1", valid=True, doc_valid=True,
                            answer_valid=True, graph_valid=True),
        "q2": ReviewVerdict(id="q2", valid=True, doc_valid=True,
                            answer_valid=False, graph_valid=False),
        # q3 미라벨 (분모 제외)
    }
    res = generator_precision(gold, verdicts)
    assert res["n_total"] == 3
    assert res["n_reviewed"] == 2
    # 전체: 2개 라벨, 둘 다 valid=True → 1.0
    assert res["overall"]["precision"] == 1.0
    assert res["overall"]["n_labeled"] == 2
    # doc: 둘 다 True → 1.0
    assert res["by_unit"]["doc"]["precision"] == 1.0
    # answer: True, False → 0.5
    assert res["by_unit"]["answer"]["precision"] == 0.5
    # graph: True, False → 0.5
    assert res["by_unit"]["graph"]["precision"] == 0.5
    # 단위 서빙 항목 수 (q1,q2,q3 모두 3단위 서빙)
    assert res["by_unit"]["graph"]["n_serving"] == 3


def test_generator_precision_answerable_ratio() -> None:
    gold = GoldSet(items=[
        _sg_item("q1", answerable=True),
        _sg_item("q2", answerable=False),
    ])
    res = generator_precision(gold, {})
    assert res["answerable_ratio"] == 0.5
    assert res["n_reviewed"] == 0
    assert res["overall"]["n_labeled"] == 0


def test_generator_precision_unit_excludes_non_serving() -> None:
    """graph 미서빙(속성 사실) 항목은 graph 정밀도 분모에서 빠진다."""
    attr = GoldItem(
        id="q1", query="?", relevant_doc_ids=[5],
        reference_answer="100만원", measurement_units=["doc", "answer"],
    )
    gold = GoldSet(items=[attr])
    verdicts = {"q1": ReviewVerdict(id="q1", valid=True, graph_valid=True)}
    res = generator_precision(gold, verdicts)
    # graph 서빙 0 → labeled 0 (graph_valid 가 채워졌어도 서빙 안 하면 무시)
    assert res["by_unit"]["graph"]["n_serving"] == 0
    assert res["by_unit"]["graph"]["n_labeled"] == 0


def test_generator_precision_end_to_end(tmp_path: Path) -> None:
    """export → 채움 → import → score 전체 흐름."""
    gold = GoldSet(items=[_sg_item("q1"), _sg_item("q2")])
    path = tmp_path / "review.csv"
    export_review_csv(gold, path)
    # 사람이 채운 것을 시뮬레이션 — 파일 재작성
    rows = []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows[0]["human_valid"] = "1"
    rows[0]["human_doc_valid"] = "1"
    rows[1]["human_valid"] = "0"
    rows[1]["human_doc_valid"] = "0"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(REVIEW_CSV_COLUMNS))
        w.writeheader()
        w.writerows(rows)
    verdicts = import_review_csv(path)
    res = generator_precision(gold, verdicts)
    assert res["overall"]["precision"] == 0.5  # 1 valid / 2 labeled
    assert res["by_unit"]["doc"]["precision"] == 0.5
