"""eval_search 헬퍼 단위 테스트 — S1-5(실패행 graph 키), S1-6(per-pair 증거).

검증 항목:
  (i)   ``_failed_metric_keys`` 가 chunk 키는 항상, graph 키는 has_graph 일 때만
        None 으로 채운다 (chunk-only 질의는 graph 키 미포함).
  (ii)  ``_build_match_pairs`` 가 MatchReport.results 로부터 per-pair 증거를
        JSON 직렬화 가능 list 로 구성한다.
  (iii) graph_match_pairs / graph_match_tiers 등 list/dict 값은 CI 대상에서
        자연 제외된다 (_is_ci_metric).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
if str(_PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import eval_search  # type: ignore[import-not-found]  # noqa: E402

from context_loop.eval.gold_set import GraphEntityRef  # noqa: E402
from context_loop.eval.graph_match import run_entity_matching  # noqa: E402


def _embed(_text: str) -> list[float] | None:
    return None  # T4 비활성 — 표면 tier 만으로 테스트


# ---------------------------------------------------------------------------
# S1-5 — 실패 질의 row 의 표준 메트릭 None 키
# ---------------------------------------------------------------------------


def test_failed_metric_keys_chunk_only() -> None:
    """chunk-only 실패 질의 — chunk 키만 None, graph 키 미포함."""
    keys = eval_search._failed_metric_keys(5, has_graph=False)
    assert keys["recall@5"] is None
    assert keys["precision@5"] is None
    assert keys["hit@5"] is None
    assert keys["ndcg@5"] is None
    assert keys["mrr"] is None
    # graph 정답 없는 질의는 graph 키를 넣지 않는다 (분류 로직 존중).
    assert not any(k.startswith("graph_") for k in keys)


def test_failed_metric_keys_graph_item() -> None:
    """graph 정답 보유 실패 질의 — 수치 graph 메트릭도 None 명시."""
    keys = eval_search._failed_metric_keys(5, has_graph=True)
    for k in (
        "graph_recall@5", "graph_recall_surface@5",
        "graph_precision@5", "graph_precision_surface@5",
        "graph_hit@5", "graph_hit_surface@5",
        "graph_ndcg@5", "graph_ndcg_surface@5",
        "graph_mrr", "graph_mrr_surface",
    ):
        assert k in keys, f"{k} 누락"
        assert keys[k] is None
    # chunk 키도 함께 존재.
    assert keys["recall@5"] is None


def test_failed_graph_keys_excluded_from_average() -> None:
    """실패행의 graph None 키는 aggregate 평균에서 자동 스킵된다."""
    rows = [
        {
            "mode": "graph",
            "graph_recall@5": 1.0,
            "metric_failed": False,
        },
        {
            "mode": "graph",
            "metric_failed": True,
            **eval_search._failed_metric_keys(5, has_graph=True),
        },
    ]
    from context_loop.eval.metrics import aggregate

    out = aggregate(rows)
    # None 행 제외 → 성공 1개의 1.0 만 평균.
    assert out["graph_recall@5"] == 1.0


# ---------------------------------------------------------------------------
# S1-6 — per-pair 매칭 증거
# ---------------------------------------------------------------------------


def _ge(name: str, type_: str) -> GraphEntityRef:
    return GraphEntityRef(name=name, type=type_)


def test_build_match_pairs_records_matched_goldens() -> None:
    """매칭된 골든마다 golden_name/type/retrieved_index/tier/score 기록."""
    golden = [
        _ge("인증 서비스", "system"),  # 매칭
        _ge("없는 엔티티", "system"),  # 미매칭 → 제외
    ]
    retrieved = [_ge("인증 서비스", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    pairs = eval_search._build_match_pairs(golden, report)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["golden_name"] == "인증 서비스"
    assert pair["golden_type"] == "system"
    assert pair["retrieved_index"] == 0
    assert pair["tier"] == "exact"
    assert pair["score"] == 1.0


def test_build_match_pairs_empty_when_no_match() -> None:
    """전부 미매칭이면 빈 list."""
    golden = [_ge("A", "system")]
    retrieved = [_ge("B", "service")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert eval_search._build_match_pairs(golden, report) == []


def test_match_pairs_not_a_ci_metric() -> None:
    """graph_match_pairs(list) 는 CI 집계 대상에서 자연 제외된다."""
    rows = [
        {
            "mode": "graph",
            "metric_failed": False,
            "graph_recall@5": 1.0,
            "graph_match_pairs": [
                {"golden_name": "X", "golden_type": "system",
                 "retrieved_index": 0, "tier": "exact", "score": 1.0},
            ],
        },
    ]
    cis = eval_search._chunk_metric_cis(rows)
    assert "graph_match_pairs" not in cis
    # 수치 graph 메트릭은 CI 에 포함.
    assert "graph_recall@5" in cis


# ---------------------------------------------------------------------------
# source-grounded (PR #79 P4) — 측정 단위 일급화 + answerable 위생
# ---------------------------------------------------------------------------


def _gold_item(**kw):  # type: ignore[no-untyped-def]
    from context_loop.eval.gold_set import GoldItem
    base = {"id": "q1", "query": "?"}
    base.update(kw)
    return GoldItem(**base)


def test_serves_unit_explicit_measurement_units() -> None:
    item = _gold_item(measurement_units=["doc", "graph"])
    assert eval_search._serves_unit(item, "doc") is True
    assert eval_search._serves_unit(item, "graph") is True
    assert eval_search._serves_unit(item, "answer") is False


def test_serves_unit_legacy_inference() -> None:
    """measurement_units 가 비면 정답키 보유로 단위를 추론한다."""
    item = _gold_item(relevant_doc_ids=[1])
    assert eval_search._serves_unit(item, "doc") is True
    assert eval_search._serves_unit(item, "graph") is False
    item2 = _gold_item(relevant_doc_ids=[], reference_answer="답")
    assert eval_search._serves_unit(item2, "answer") is True
    assert eval_search._serves_unit(item2, "doc") is False


def test_write_summary_answerable_hygiene(tmp_path: Path) -> None:
    """answerable=False 행은 메트릭 평균 분모에서 제외되고 별도 보고된다."""
    rows = [
        {"id": "a", "mode": "chunk", "recall@5": 1.0,
         "measurement_units": ["doc"], "answerable": True},
        {"id": "b", "mode": "chunk", "recall@5": 1.0,
         "measurement_units": ["doc"], "answerable": True},
        # 회수 불가 표적 — recall 0 이지만 분모에서 제외돼야 함
        {"id": "c", "mode": "chunk", "recall@5": 0.0,
         "measurement_units": ["doc"], "answerable": False},
    ]
    out = eval_search.write_summary(
        rows, tmp_path / "s.summary.json",
        label="t", config_summary={},
    )
    # answerable=2개만 평균 → recall 1.0 (c 의 0.0 제외)
    assert out["metrics"]["recall@5"] == 1.0
    assert out["n_unanswerable"] == 1
    assert out["unanswerable_ids"] == ["c"]
    assert out["measurement_unit_coverage"]["doc"] == 3


def test_write_summary_legacy_rows_unchanged(tmp_path: Path) -> None:
    """answerable/measurement_units 없는 레거시 행은 전부 분모에 포함(무변경)."""
    rows = [
        {"id": "a", "mode": "chunk", "recall@5": 1.0},
        {"id": "b", "mode": "chunk", "recall@5": 0.0},
    ]
    out = eval_search.write_summary(
        rows, tmp_path / "s.summary.json",
        label="t", config_summary={},
    )
    assert out["metrics"]["recall@5"] == 0.5  # 둘 다 포함
    assert out["n_unanswerable"] == 0
    assert out["measurement_unit_coverage"] == {"doc": 0, "answer": 0, "graph": 0}


def test_context_recall_is_ci_metric() -> None:
    """context_recall@k 가 bootstrap CI 대상에 포함된다."""
    rows = [
        {"context_recall@5": 1.0}, {"context_recall@5": 0.0},
        {"context_recall@5": 1.0}, {"context_recall@5": 1.0},
    ]
    cis = eval_search._chunk_metric_cis(rows)
    assert "context_recall@5" in cis
    assert "mean" in cis["context_recall@5"]


# ---------------------------------------------------------------------------
# source-grounded (PR #79 P4-answer) — 답변 단위 채점 + divergence
# ---------------------------------------------------------------------------


class _StubLLM:
    def __init__(self, responses):  # type: ignore[no-untyped-def]
        self._responses = list(responses)
        self.calls = []  # type: ignore[var-annotated]

    async def complete(self, prompt, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"prompt": prompt, **kwargs})
        return self._responses.pop(0) if self._responses else ""


def test_normalize_answer_strips_punct_and_case() -> None:
    assert eval_search._normalize_answer('  "100만원."  ') == "100만원"
    assert eval_search._normalize_answer("Token Validator") == "token validator"


def test_factoid_match_exact_and_contains() -> None:
    assert eval_search.factoid_match("100만원", "약 100만원 입니다") is True
    assert eval_search.factoid_match("Token Validator", "token validator") is True
    assert eval_search.factoid_match("100만원", "50만원") is False
    assert eval_search.factoid_match("", "무엇") is False


@pytest.mark.asyncio
async def test_score_answer_correctness_factoid_no_llm() -> None:
    """팩토이드 일치는 LLM 호출 없이 1.0."""
    judge = _StubLLM([])
    score, method, _ = await eval_search.score_answer_correctness(
        "q", "100만원", "정답은 100만원이다", judge=judge,
    )
    assert score == 1.0
    assert method == "factoid"
    assert judge.calls == []  # judge 미호출


@pytest.mark.asyncio
async def test_score_answer_correctness_judge_path() -> None:
    """팩토이드 불일치 → judge 0~5 → 0..1 정규화."""
    judge = _StubLLM(['{"score": 4, "reason": "대체로 맞음"}'])
    score, method, reason = await eval_search.score_answer_correctness(
        "q", "토큰 검증 모듈에 의존", "인증은 검증기에 기댄다", judge=judge,
    )
    assert score == 0.8
    assert method == "judge"
    assert reason == "대체로 맞음"


@pytest.mark.asyncio
async def test_score_answer_correctness_parse_error() -> None:
    judge = _StubLLM(["깨진 응답"])
    score, method, _ = await eval_search.score_answer_correctness(
        "q", "기준", "전혀 다른 답", judge=judge,
    )
    assert score == -1.0
    assert method == "parse_error"


@pytest.mark.asyncio
async def test_generate_answer_from_context_uses_system_llm() -> None:
    llm = _StubLLM(["Token Validator에 의존한다."])
    out = await eval_search.generate_answer_from_context(
        "질문", "검색 컨텍스트 본문", answer_llm=llm,
    )
    assert out == "Token Validator에 의존한다."
    assert llm.calls[0]["purpose"] == "goldset_answer_gen"
    assert "검색 컨텍스트 본문" in llm.calls[0]["prompt"]


def test_divergence_label_axes() -> None:
    # 답 맞음 + 검색 실패 → answer_without_context
    assert eval_search._divergence_label(0.0, 1) == "answer_without_context"
    # 검색 OK + 답 실패 → context_without_answer
    assert eval_search._divergence_label(1.0, 0) == "context_without_answer"
    # 두 축 일치 → 빈 라벨
    assert eval_search._divergence_label(1.0, 1) == ""
    assert eval_search._divergence_label(0.0, 0) == ""
    # 판정 불가
    assert eval_search._divergence_label(None, 1) == ""
    assert eval_search._divergence_label(0.0, None) == ""


def test_write_summary_answer_and_divergence_report(tmp_path: Path) -> None:
    rows = [
        {"id": "a", "mode": "chunk", "answer_correct": 1, "answer_correctness": 1.0,
         "measurement_units": ["doc", "answer"], "answerable": True,
         "divergence": "answer_without_context"},
        {"id": "b", "mode": "chunk", "answer_correct": 0, "answer_correctness": 0.2,
         "measurement_units": ["doc", "answer"], "answerable": True,
         "divergence": "context_without_answer"},
        {"id": "c", "mode": "chunk", "answer_correct": 1, "answer_correctness": 0.8,
         "measurement_units": ["doc", "answer"], "answerable": True},
        {"id": "d", "mode": "chunk", "answer_parse_failed": True,
         "measurement_units": ["answer"], "answerable": True},
    ]
    out = eval_search.write_summary(
        rows, tmp_path / "s.summary.json", label="t", config_summary={},
    )
    # answer_correct 평균 = (1+0+1)/3
    assert abs(out["metrics"]["answer_correct"] - (2 / 3)) < 1e-9
    assert out["n_answer_scored"] == 3
    assert out["n_answer_parse_failed"] == 1
    assert out["divergence_counts"]["answer_without_context"] == 1
    assert out["divergence_counts"]["context_without_answer"] == 1
