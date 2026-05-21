"""가상 질문 생성기 (R3) 단위 테스트."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from context_loop.ingestion.confluence_extractor import ExtractedDocument, Section
from context_loop.processor.question_generator import (
    InputTooLargeError,
    QuestionGenConfig,
    generate_questions_for_document,
)


def _llm_returning(payload: Any) -> AsyncMock:
    response = json.dumps(payload, ensure_ascii=False)
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=response)
    return mock


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
async def test_oversized_input_raises_input_too_large() -> None:
    """문서 본문 토큰이 max_input_tokens 초과면 InputTooLargeError."""
    llm = _llm_returning({"sections": []})
    cfg = QuestionGenConfig(max_input_tokens=5)
    extracted = _extracted([
        (1, "AuthService", "AuthService TokenStore Cache ProxyLayer EventBus"),
    ])
    with pytest.raises(InputTooLargeError):
        await generate_questions_for_document(
            doc_title="아키텍처", extracted=extracted, llm_client=llm, config=cfg,
        )
    llm.complete.assert_not_called()


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
