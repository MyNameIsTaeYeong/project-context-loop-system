"""가상 질문 생성기 (R3) 단위 테스트."""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock

import pytest

from context_loop.ingestion.confluence_extractor import ExtractedDocument, Section
from context_loop.processor.question_generator import (
    QuestionGenConfig,
    QuestionGenStats,
    _plan_section_batches,
    generate_questions_for_document,
)


def _llm_returning(payload: Any) -> AsyncMock:
    response = json.dumps(payload, ensure_ascii=False)
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=response)
    return mock


def _batch_aware_llm(question_for: Any) -> AsyncMock:
    """프롬프트에 담긴 section_index 를 파싱해 그 섹션의 질문을 돌려주는 mock.

    배치 폴백은 배치마다 서로 다른 섹션을 담아 여러 번 호출하므로, 호출
    순서에 의존하지 않고 프롬프트 내용으로 응답을 결정한다 (asyncio.gather
    스케줄링 순서 비의존).
    """
    async def fake(prompt: str, **_kwargs: Any) -> str:
        idxs = [int(m) for m in re.findall(r"section_index=(-?\d+)", prompt)]
        secs = [
            {"section_index": i, "questions": list(question_for(i))} for i in idxs
        ]
        return json.dumps({"sections": secs}, ensure_ascii=False)

    mock = AsyncMock()
    mock.complete = AsyncMock(side_effect=fake)
    return mock


def _big_body(marker: str, n_words: int = 400) -> str:
    """토큰 수가 충분히 큰(배치 예산 초과 유도용) 섹션 본문을 만든다."""
    return " ".join(f"{marker}word{i}" for i in range(n_words))


def _extracted(sections: list[tuple[int, str, str]]) -> ExtractedDocument:
    """헬퍼 — (level, title, md_content) 튜플로 ExtractedDocument 생성."""
    section_objs = []
    for level, title, body in sections:
        section_objs.append(Section(
            level=level,
            title=title,
            anchor=title.lower().replace(" ", "-"),
            path=[title],
            md_content=body,
        ))
    plain = "\n\n".join(f"# {t}\n\n{b}" for _, t, b in sections)
    return ExtractedDocument(plain_text=plain, sections=section_objs)


# ---------------------------------------------------------------------------
# 가드
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_doc_title_returns_empty() -> None:
    llm = _llm_returning({"sections": []})
    extracted = _extracted([(1, "A", "본문")])
    result, stats = await generate_questions_for_document(
        doc_title="", extracted=extracted, llm_client=llm,
    )
    assert result == {}
    assert stats.llm_called is False
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_empty_extracted_returns_empty() -> None:
    llm = _llm_returning({"sections": []})
    extracted = ExtractedDocument(plain_text="", sections=[])
    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm,
    )
    assert result == {}
    assert stats.sections_total == 0
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_input_triggers_batched_fallback() -> None:
    """(R-3) 사전 가드 초과면 raise 대신 섹션 배치 분할 폴백으로 self-heal.

    이전엔 InputTooLargeError 를 raise 했으나, 이제 여러 섹션을 한도 이하
    배치로 나눠 호출하고 병합한다.
    """
    cfg = QuestionGenConfig(max_input_tokens=5)  # 사전 가드 강제 초과
    extracted = _extracted([
        (1, "S0", _big_body("s0")),
        (1, "S1", _big_body("s1")),
        (1, "S2", _big_body("s2")),
    ])
    llm = _batch_aware_llm(lambda i: [f"섹션 {i} 질문입니까?"])

    result, stats = await generate_questions_for_document(
        doc_title="아키텍처", extracted=extracted, llm_client=llm, config=cfg,
    )

    assert stats.fallback_used is True
    assert stats.batch_count >= 2
    assert llm.complete.await_count == stats.batch_count
    # 세 섹션 모두 질문이 생성되어 병합됨
    assert result == {
        0: ["섹션 0 질문입니까?"],
        1: ["섹션 1 질문입니까?"],
        2: ["섹션 2 질문입니까?"],
    }
    assert stats.final_questions == 3


@pytest.mark.asyncio
async def test_batched_dedup_across_batches() -> None:
    """서로 다른 배치가 동일 질문을 반환하면 seen_global 로 1개만 유지."""
    cfg = QuestionGenConfig(max_input_tokens=5)
    extracted = _extracted([
        (1, "S0", _big_body("s0")),
        (1, "S1", _big_body("s1")),
        (1, "S2", _big_body("s2")),
    ])
    # 모든 섹션이 같은 질문 텍스트를 반환 → 문서 전체 1개만 남아야 함
    llm = _batch_aware_llm(lambda i: ["공유되는 동일 질문?"])

    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm, config=cfg,
    )

    assert stats.batch_count >= 2
    total = sum(len(qs) for qs in result.values())
    assert total == 1
    assert stats.final_questions == 1


@pytest.mark.asyncio
async def test_batched_respects_max_questions_per_doc() -> None:
    """배치 합산이 상한을 넘어도 문서 전체 상한에서 컷."""
    cfg = QuestionGenConfig(max_input_tokens=5, max_questions_per_doc=2)
    extracted = _extracted([
        (1, "S0", _big_body("s0")),
        (1, "S1", _big_body("s1")),
        (1, "S2", _big_body("s2")),
    ])
    llm = _batch_aware_llm(
        lambda i: [f"섹션 {i} 질문 {j}?" for j in range(5)],
    )

    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm, config=cfg,
    )

    assert sum(len(qs) for qs in result.values()) == 2
    assert stats.final_questions == 2


@pytest.mark.asyncio
async def test_single_section_over_budget_truncated() -> None:
    """단일 거대 섹션이 예산 초과면 절단 후 1배치 호출, sections_truncated=1."""
    cfg = QuestionGenConfig(max_input_tokens=5)
    # MIN_BUDGET(512) 를 넘기는 아주 큰 단일 섹션
    extracted = _extracted([(1, "Huge", _big_body("h", n_words=2000))])
    llm = _batch_aware_llm(lambda i: [f"거대 섹션 {i} 질문?"])

    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm, config=cfg,
    )

    assert stats.fallback_used is True
    assert stats.batch_count == 1
    assert stats.sections_truncated == 1
    assert result == {0: ["거대 섹션 0 질문?"]}


@pytest.mark.asyncio
async def test_api_context_error_triggers_batched_fallback() -> None:
    """첫 전체 호출이 API 컨텍스트 초과면 배치 경로로 self-heal."""
    # 문서는 사전 가드 이하 → 단일 전체 호출 시도 → 그 호출이 컨텍스트 초과.
    extracted = _extracted([
        (1, "S0", "AuthService 는 토큰을 검증한다."),
        (1, "S1", "TokenStore 는 토큰을 저장한다."),
    ])

    call_count = {"n": 0}

    async def fake(prompt: str, **_kwargs: Any) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError(
                "This model's maximum context length is 4096 tokens",
            )
        idxs = [int(m) for m in re.findall(r"section_index=(-?\d+)", prompt)]
        secs = [
            {"section_index": i, "questions": [f"섹션 {i} 질문?"]} for i in idxs
        ]
        return json.dumps({"sections": secs}, ensure_ascii=False)

    mock = AsyncMock()
    mock.complete = AsyncMock(side_effect=fake)

    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=mock,
    )

    assert stats.fallback_used is True
    assert stats.llm_failed is False
    assert result  # 배치 경로로 질문이 채워짐
    assert stats.final_questions >= 1


@pytest.mark.asyncio
async def test_generic_api_error_still_returns_empty() -> None:
    """첫 전체 호출이 일반 예외면 폴백 아님 — 빈 dict + llm_failed=True (기존 계약)."""
    extracted = _extracted([(1, "A", "본문")])
    mock = AsyncMock()
    mock.complete = AsyncMock(side_effect=Exception("network boom"))

    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=mock,
    )

    assert result == {}
    assert stats.llm_failed is True
    assert stats.fallback_used is False


def test_plan_section_batches_deterministic() -> None:
    """순수 함수: 문서 순서 유지 + 각 배치 예산 이하 + 반복 시 동일 결과."""
    from context_loop.processor.chunker import count_tokens

    cfg = QuestionGenConfig(max_input_tokens=5)  # budget → MIN_BUDGET(512)
    payload = [
        (0, _big_body("a", n_words=300)),
        (1, _big_body("b", n_words=300)),
        (2, _big_body("c", n_words=300)),
    ]

    batches_1 = _plan_section_batches(
        payload, doc_title="d", cfg=cfg, stats=QuestionGenStats(),
    )
    batches_2 = _plan_section_batches(
        payload, doc_title="d", cfg=cfg, stats=QuestionGenStats(),
    )

    # 결정론
    assert batches_1 == batches_2
    # 여러 배치로 쪼개짐
    assert len(batches_1) >= 2
    # 문서 순서 보존 (평탄화한 section_index 가 0,1,2)
    flat = [idx for batch in batches_1 for idx, _ in batch]
    assert flat == [0, 1, 2]
    # 각 배치 렌더 비용이 예산 이하 (단독 절단 배치 제외 — 여기선 절단 없음)
    budget = 512
    for batch in batches_1:
        cost = sum(
            count_tokens(f"--- section_index={idx} ---\n{text}")
            for idx, text in batch
        )
        assert cost <= budget


# ---------------------------------------------------------------------------
# LLM 호출 결과 처리
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_call_extracts_questions_per_section() -> None:
    """LLM 1회 호출로 섹션별 질문 매핑을 반환."""
    payload = {
        "sections": [
            {
                "section_index": 0,
                "section_path": "AuthService",
                "questions": [
                    "AuthService 는 어떻게 토큰을 검증하나요?",
                    "토큰 만료 시 AuthService 의 동작은?",
                ],
            },
            {
                "section_index": 1,
                "section_path": "TokenStore",
                "questions": ["TokenStore 의 인덱스 구조는?"],
            },
        ],
    }
    llm = _llm_returning(payload)
    extracted = _extracted([
        (1, "AuthService", "AuthService 는 토큰을 검증한다."),
        (1, "TokenStore", "TokenStore 는 토큰을 저장한다."),
    ])

    result, stats = await generate_questions_for_document(
        doc_title="아키텍처", extracted=extracted, llm_client=llm,
    )

    assert llm.complete.await_count == 1
    assert result == {
        0: [
            "AuthService 는 어떻게 토큰을 검증하나요?",
            "토큰 만료 시 AuthService 의 동작은?",
        ],
        1: ["TokenStore 의 인덱스 구조는?"],
    }
    assert stats.sections_total == 2
    assert stats.sections_with_questions == 2
    assert stats.final_questions == 3
    assert stats.llm_failed is False


@pytest.mark.asyncio
async def test_invalid_section_index_dropped() -> None:
    """section_index 가 입력 섹션 목록에 없으면 그 섹션 질문은 전부 drop."""
    payload = {
        "sections": [
            {"section_index": 0, "questions": ["유효 질문?"]},
            {"section_index": 99, "questions": ["무효 섹션 질문?", "또?"]},
        ],
    }
    llm = _llm_returning(payload)
    extracted = _extracted([(1, "A", "본문")])
    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm,
    )

    assert result == {0: ["유효 질문?"]}
    assert stats.dropped_questions == 2


@pytest.mark.asyncio
async def test_duplicate_questions_dropped_within_doc() -> None:
    """같은 문서 안에서 (대소문자 무관) 중복 질문은 drop."""
    payload = {
        "sections": [
            {"section_index": 0, "questions": ["같은 질문?", "다른 질문?"]},
            {"section_index": 1, "questions": ["같은 질문?", "또 다른?"]},
        ],
    }
    llm = _llm_returning(payload)
    extracted = _extracted([
        (1, "A", "본문 A"),
        (1, "B", "본문 B"),
    ])
    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm,
    )

    assert result == {0: ["같은 질문?", "다른 질문?"], 1: ["또 다른?"]}
    assert stats.dropped_questions == 1


@pytest.mark.asyncio
async def test_short_or_invalid_questions_dropped() -> None:
    """4글자 미만 또는 비-문자열 질문은 drop."""
    payload = {
        "sections": [{
            "section_index": 0,
            "questions": ["짧", 123, "정상 길이의 질문?", "  "],
        }],
    }
    llm = _llm_returning(payload)
    extracted = _extracted([(1, "A", "본문")])
    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm,
    )

    assert result == {0: ["정상 길이의 질문?"]}
    assert stats.dropped_questions == 3


@pytest.mark.asyncio
async def test_llm_json_parse_failure_returns_empty() -> None:
    """LLM 응답이 JSON 으로 파싱 안 되면 빈 dict + llm_failed=True."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value="이건 JSON 이 아닙니다")
    extracted = _extracted([(1, "A", "본문")])

    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=mock,
    )

    assert result == {}
    assert stats.llm_failed is True
    assert stats.llm_called is True


@pytest.mark.asyncio
async def test_max_questions_per_doc_caps_output() -> None:
    """max_questions_per_doc 한도가 적용된다."""
    payload = {
        "sections": [{
            "section_index": 0,
            "questions": [f"질문 {i}?" for i in range(20)],
        }],
    }
    llm = _llm_returning(payload)
    extracted = _extracted([(1, "A", "본문")])
    cfg = QuestionGenConfig(max_questions_per_doc=5)
    result, stats = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm, config=cfg,
    )

    assert sum(len(qs) for qs in result.values()) == 5
    assert stats.final_questions == 5


@pytest.mark.asyncio
async def test_sectionless_document_uses_negative_one_index() -> None:
    """sections 가 없는 plain 문서는 section_index=-1 로 매핑."""
    payload = {
        "sections": [{
            "section_index": -1,
            "questions": ["전체 본문 질문?"],
        }],
    }
    llm = _llm_returning(payload)
    extracted = ExtractedDocument(plain_text="플레인 본문 텍스트", sections=[])

    result, _ = await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm,
    )

    assert result == {-1: ["전체 본문 질문?"]}


@pytest.mark.asyncio
async def test_prompt_disables_thinking_mode() -> None:
    """reasoning_mode='off' 로 thinking 비활성화 의도 전달."""
    llm = _llm_returning({"sections": []})
    extracted = _extracted([(1, "A", "본문")])
    await generate_questions_for_document(
        doc_title="d", extracted=extracted, llm_client=llm,
    )
    kwargs = llm.complete.await_args.kwargs
    assert kwargs.get("reasoning_mode") == "off"
    assert kwargs.get("purpose") == "question_generation"


@pytest.mark.asyncio
async def test_prompt_includes_doc_title_and_section_text() -> None:
    """프롬프트에 doc_title 과 섹션 본문이 모두 포함되어 LLM 이 사용할 수 있어야 한다."""
    llm = _llm_returning({"sections": []})
    extracted = _extracted([
        (1, "AuthMarker", "본문 텍스트 ZZZ"),
    ])
    await generate_questions_for_document(
        doc_title="아키텍처-제목", extracted=extracted, llm_client=llm,
    )
    user_prompt = llm.complete.await_args.args[0]
    assert "아키텍처-제목" in user_prompt
    assert "AuthMarker" in user_prompt
    assert "ZZZ" in user_prompt
    # section_index 정수가 LLM 입력에 명시되어야 매핑 가능
    assert "section_index=0" in user_prompt
