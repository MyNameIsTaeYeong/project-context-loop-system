"""LLM 본문 추출기 테스트."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.extraction_unit import ExtractionUnit
from context_loop.processor.llm_body_extractor import (
    LLMBodyExtractionConfig,
    extract_llm_body_graph,
)


def _unit(
    *,
    body: str = "본문",
    section_path: tuple[str, ...] = ("Root",),
    document_id: int = 1,
    ordinal: int = 0,
    token_count: int = 500,
    split_part: int = 0,
    split_total: int = 1,
) -> ExtractionUnit:
    sid = f"{document_id}:{ordinal}"
    return ExtractionUnit(
        unit_id=f"{document_id}:{ordinal:04d}",
        document_id=document_id,
        ordinal=ordinal,
        section_ids=(sid,),
        primary_section_id=sid,
        section_path=section_path,
        breadcrumb="",
        content=body,
        body=body,
        token_count=token_count,
        has_table=False,
        has_code_block=False,
        split_part=split_part,
        split_total=split_total,
    )


def _llm_returning(payload: Any) -> AsyncMock:
    """단일 응답 stub. JSON 으로 직렬화해서 LLM 응답처럼 만든다."""
    response = json.dumps(payload, ensure_ascii=False)
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=response)
    return mock


def _llm_with_responses(responses: list[Any]) -> AsyncMock:
    """unit 별로 다른 응답을 stub. 호출 순서대로 소비."""
    serialized = [json.dumps(r, ensure_ascii=False) for r in responses]
    mock = AsyncMock()
    mock.complete = AsyncMock(side_effect=serialized)
    return mock


# ---------------------------------------------------------------------------
# 가드
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_units_returns_empty() -> None:
    llm = _llm_returning({"entities": [], "relations": []})
    g, stats = await extract_llm_body_graph([], doc_title="d", llm_client=llm)
    assert g.entities == [] and g.relations == []
    llm.complete.assert_not_called()
    assert stats.units_total == 0


@pytest.mark.asyncio
async def test_empty_doc_title_returns_empty() -> None:
    llm = _llm_returning({"entities": [], "relations": []})
    g, _ = await extract_llm_body_graph(
        [_unit()], doc_title="", llm_client=llm,
    )
    assert g.entities == [] and g.relations == []
    llm.complete.assert_not_called()


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_units_are_gated_out() -> None:
    """token_count < min_unit_tokens 인 unit 은 LLM 호출 안 함."""
    llm = _llm_returning({"entities": [], "relations": []})
    cfg = LLMBodyExtractionConfig(min_unit_tokens=200)
    units = [_unit(token_count=50, ordinal=i) for i in range(3)]
    g, stats = await extract_llm_body_graph(
        units, doc_title="d", llm_client=llm, config=cfg,
    )
    assert g.entities == []
    llm.complete.assert_not_called()
    assert stats.units_skipped_short == 3
    assert stats.units_called == 0


@pytest.mark.asyncio
async def test_split_overlap_parts_are_gated_out() -> None:
    """split_part > 0 인 unit (중복 추출 방지) 은 LLM 호출 안 함."""
    llm = _llm_returning({"entities": [], "relations": []})
    units = [
        _unit(token_count=500, ordinal=0, split_part=0, split_total=3),
        _unit(token_count=500, ordinal=1, split_part=1, split_total=3),
        _unit(token_count=500, ordinal=2, split_part=2, split_total=3),
    ]
    _, stats = await extract_llm_body_graph(units, doc_title="d", llm_client=llm)
    assert llm.complete.await_count == 1
    assert stats.units_skipped_overlap == 2


@pytest.mark.asyncio
async def test_max_units_per_doc_caps_calls() -> None:
    llm = _llm_returning({"entities": [], "relations": []})
    cfg = LLMBodyExtractionConfig(max_units_per_doc=2)
    units = [_unit(token_count=500, ordinal=i) for i in range(5)]
    await extract_llm_body_graph(units, doc_title="d", llm_client=llm, config=cfg)
    assert llm.complete.await_count == 2


# ---------------------------------------------------------------------------
# 정상 추출
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_entities_and_relations_extracted() -> None:
    payload = {
        "entities": [
            {"name": "Auth Service", "type": "system", "description": "인증 담당"},
            {"name": "Token Validator", "type": "module"},
        ],
        "relations": [
            {"source": "Auth Service", "target": "Token Validator",
             "type": "depends_on"},
        ],
    }
    llm = _llm_returning(payload)
    units = [_unit(section_path=("Arch", "Auth"))]
    g, stats = await extract_llm_body_graph(units, doc_title="d", llm_client=llm)

    types = {(e.name, e.entity_type) for e in g.entities}
    assert ("Auth Service", "system") in types
    assert ("Token Validator", "module") in types

    rel = g.relations[0]
    assert rel.source == "Auth Service"
    assert rel.target == "Token Validator"
    assert rel.relation_type == "depends_on"
    # label 에 첫 등장 unit 의 section_path 가 기록
    assert rel.label == "Arch > Auth"

    assert stats.final_entities == 2
    assert stats.final_relations == 1
    assert stats.dropped_entities == 0
    assert stats.dropped_relations == 0


@pytest.mark.asyncio
async def test_description_is_preserved() -> None:
    payload = {
        "entities": [
            {"name": "X", "type": "system", "description": "도메인 X"},
        ],
        "relations": [],
    }
    llm = _llm_returning(payload)
    g, _ = await extract_llm_body_graph(
        [_unit()], doc_title="d", llm_client=llm,
    )
    assert g.entities == []  # 관계 없이는 emit 안 함
    # 하지만 통계는 채워짐 (raw 단계)
    # description 보존 검증을 위해 관계 있는 케이스 따로 작성


# ---------------------------------------------------------------------------
# 어휘 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disallowed_entity_type_dropped() -> None:
    """어휘 외 타입 엔티티는 드롭, 그 끝점을 가진 관계도 드롭.

    유효 관계가 하나도 남지 않으면 그래프 자체가 빈다 (link/body extractor 와
    동일 정책).
    """
    payload = {
        "entities": [
            {"name": "Good", "type": "system"},
            {"name": "Bad", "type": "made_up_type"},
        ],
        "relations": [
            {"source": "Good", "target": "Bad", "type": "depends_on"},
        ],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)
    # 어휘 외 entity 1 + 그 entity 를 끝점으로 하는 relation 1 → 양쪽 모두 드롭
    assert stats.dropped_entities == 1
    assert stats.dropped_relations == 1
    # 유효 관계가 0 → 빈 그래프 (Good entity 도 emit 안 됨)
    assert g.entities == [] and g.relations == []


@pytest.mark.asyncio
async def test_disallowed_relation_type_dropped() -> None:
    payload = {
        "entities": [
            {"name": "A", "type": "system"},
            {"name": "B", "type": "system"},
        ],
        "relations": [
            {"source": "A", "target": "B", "type": "blesses"},
            {"source": "A", "target": "B", "type": "depends_on"},
        ],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)
    rtypes = {r.relation_type for r in g.relations}
    assert rtypes == {"depends_on"}
    assert stats.dropped_relations == 1


@pytest.mark.asyncio
async def test_relation_endpoint_not_in_entities_dropped() -> None:
    """LLM 이 entities 에 없는 이름을 source/target 으로 만들면 그 관계는 드롭."""
    payload = {
        "entities": [{"name": "A", "type": "system"}],
        "relations": [
            {"source": "A", "target": "Phantom", "type": "depends_on"},
        ],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)
    assert g.relations == []
    assert stats.dropped_relations == 1


@pytest.mark.asyncio
async def test_self_loop_relation_dropped() -> None:
    payload = {
        "entities": [{"name": "A", "type": "system"}],
        "relations": [{"source": "A", "target": "A", "type": "depends_on"}],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)
    assert g.relations == []
    assert stats.dropped_relations == 1


# ---------------------------------------------------------------------------
# 다중 unit / dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_unit_entity_dedup_keeps_first_casing() -> None:
    """unit 1: 'Auth Service', unit 2: 'AUTH SERVICE' → 1개 entity (첫 표기 보존)."""
    responses = [
        {
            "entities": [{"name": "Auth Service", "type": "system"},
                         {"name": "DB", "type": "module"}],
            "relations": [
                {"source": "Auth Service", "target": "DB", "type": "depends_on"},
            ],
        },
        {
            "entities": [{"name": "AUTH SERVICE", "type": "system"},
                         {"name": "Cache", "type": "module"}],
            "relations": [
                {"source": "AUTH SERVICE", "target": "Cache", "type": "uses"},
            ],
        },
    ]
    llm = _llm_with_responses(responses)
    units = [
        _unit(section_path=("S1",), ordinal=0),
        _unit(section_path=("S2",), ordinal=1),
    ]
    g, _ = await extract_llm_body_graph(units, doc_title="d", llm_client=llm)
    auths = [e for e in g.entities if e.entity_type == "system"]
    assert len(auths) == 1
    assert auths[0].name == "Auth Service"  # 첫 등장 표기 보존
    # 관계 정규화: source 가 첫 표기로 통일
    rels_by_type = {r.relation_type: r for r in g.relations}
    assert rels_by_type["uses"].source == "Auth Service"


@pytest.mark.asyncio
async def test_cross_unit_relation_dedup() -> None:
    """같은 (source, target, type) 트리플은 한 번만."""
    same = {
        "entities": [
            {"name": "A", "type": "system"},
            {"name": "B", "type": "system"},
        ],
        "relations": [{"source": "A", "target": "B", "type": "depends_on"}],
    }
    llm = _llm_with_responses([same, same])
    units = [
        _unit(section_path=("S1",), ordinal=0),
        _unit(section_path=("S2",), ordinal=1),
    ]
    g, _ = await extract_llm_body_graph(units, doc_title="d", llm_client=llm)
    deps = [r for r in g.relations if r.relation_type == "depends_on"]
    assert len(deps) == 1
    # 첫 등장 unit (S1) label
    assert deps[0].label == "S1"


# ---------------------------------------------------------------------------
# 회복력
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unit_failure_does_not_block_others() -> None:
    """한 unit 의 LLM 호출이 실패해도 다른 unit 은 처리된다."""
    good = {
        "entities": [
            {"name": "A", "type": "system"},
            {"name": "B", "type": "system"},
        ],
        "relations": [{"source": "A", "target": "B", "type": "depends_on"}],
    }
    mock = AsyncMock()
    # 호출 순서: 첫 호출은 예외, 둘째 호출은 정상
    mock.complete = AsyncMock(
        side_effect=[Exception("network"), json.dumps(good, ensure_ascii=False)],
    )

    units = [
        _unit(section_path=("Bad",), ordinal=0),
        _unit(section_path=("Good",), ordinal=1),
    ]
    g, stats = await extract_llm_body_graph(
        units, doc_title="d", llm_client=mock,
        config=LLMBodyExtractionConfig(max_concurrency=1),
    )
    assert {e.name for e in g.entities} == {"A", "B"}
    assert stats.units_failed == 1
    assert stats.units_called == 1


@pytest.mark.asyncio
async def test_invalid_json_response_skips_unit() -> None:
    """JSON 으로 파싱 안 되는 응답은 unit 을 스킵하지만 예외 전파 안 함."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value="이건 JSON 이 아닙니다")
    units = [_unit()]
    g, stats = await extract_llm_body_graph(units, doc_title="d", llm_client=mock)
    assert g.entities == []
    assert stats.units_failed == 1


@pytest.mark.asyncio
async def test_response_not_json_object_skips_unit() -> None:
    """JSON 이지만 객체가 아닌(예: 배열) 응답은 스킵."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=json.dumps([1, 2, 3]))
    g, stats = await extract_llm_body_graph(
        [_unit()], doc_title="d", llm_client=mock,
    )
    assert g.entities == []
    assert stats.units_failed == 1


@pytest.mark.asyncio
async def test_no_relations_returns_empty_graph_even_if_entities_present() -> None:
    """엔티티만 있고 관계가 없으면 빈 그래프 반환 (link/body extractor 와 동일 정책)."""
    payload = {
        "entities": [{"name": "A", "type": "system"}],
        "relations": [],
    }
    llm = _llm_returning(payload)
    g, _ = await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)
    assert g.entities == [] and g.relations == []


# ---------------------------------------------------------------------------
# 스키마 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_entries_in_arrays_are_skipped() -> None:
    """배열 내 dict 가 아닌 항목 / 빈 이름 등은 카운트만 올리고 드롭."""
    payload = {
        "entities": [
            {"name": "", "type": "system"},     # 빈 이름
            "not a dict",                        # 잘못된 타입
            {"name": "OK", "type": "system"},
        ],
        "relations": [
            "string is not a dict",
            {"source": "OK", "target": "OK", "type": "depends_on"},  # self-loop → drop
        ],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)
    assert {e.name for e in g.entities} == set()  # 관계 0 → 빈 그래프
    assert stats.dropped_entities == 2
    assert stats.dropped_relations == 2


@pytest.mark.asyncio
async def test_custom_vocabulary_replaces_defaults() -> None:
    """allowed_*_types 를 커스터마이즈하면 그 어휘만 통과한다."""
    payload = {
        "entities": [
            {"name": "X", "type": "custom_kind"},
            {"name": "Y", "type": "system"},  # 기본 어휘이지만 custom 에는 없음
        ],
        "relations": [
            {"source": "X", "target": "Y", "type": "owns"},
        ],
    }
    llm = _llm_returning(payload)
    cfg = LLMBodyExtractionConfig(
        allowed_entity_types=("custom_kind",),
        allowed_relation_types=("owns",),
    )
    g, stats = await extract_llm_body_graph(
        [_unit()], doc_title="d", llm_client=llm, config=cfg,
    )
    # Y 는 어휘 외 → drop. 관계 끝점 누락 → 관계 drop. 유효 관계 0 → 빈 그래프.
    assert stats.dropped_entities == 1
    assert stats.dropped_relations == 1
    assert g.entities == [] and g.relations == []


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_includes_doc_title_and_unit_body() -> None:
    payload = {"entities": [], "relations": []}
    llm = _llm_returning(payload)
    body = "본문 텍스트 — 식별 가능한 마커 ZZZ"
    await extract_llm_body_graph(
        [_unit(body=body)], doc_title="제목", llm_client=llm,
    )
    user_prompt = llm.complete.await_args.args[0]
    system_prompt = llm.complete.await_args.kwargs["system"]
    assert "제목" in user_prompt
    assert "ZZZ" in user_prompt
    # 시스템 프롬프트에 어휘가 들어가 있어야 함
    assert "depends_on" in system_prompt
    assert "system" in system_prompt


@pytest.mark.asyncio
async def test_complete_call_disables_thinking_mode() -> None:
    """reasoning 모델(Qwen3/DeepSeek 등) 의 thinking 모드 비활성화 의도가 전달된다.

    빈 응답 회귀 방지: thinking 모드가 켜진 채 JSON 추출 프롬프트가 들어가면
    모델이 max_tokens 예산을 사고에 모두 쓰고 답변이 비는 문제가 있었다.
    ``graph_search_planner`` 와 동일한 처방을 적용한다. 모델별 실제 페이로드는
    ``llm.reasoning_profiles`` 설정에서 매핑하므로 호출부는 ``reasoning_mode="off"``
    의도만 넘긴다.
    """
    payload = {"entities": [], "relations": []}
    llm = _llm_returning(payload)
    await extract_llm_body_graph([_unit()], doc_title="d", llm_client=llm)

    kwargs = llm.complete.await_args.kwargs
    assert kwargs.get("reasoning_mode") == "off"
