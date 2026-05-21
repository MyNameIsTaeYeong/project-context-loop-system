"""문서 본문 → 검색용 가상 질문 생성기.

R3 — 멀티 벡터 인덱싱의 핵심 모듈. 문서를 통째로 LLM 에 넣어 각 섹션이 "답할 수
있는" 자연 질의 형태의 가상 질문을 추출한다. 이렇게 생성된 질문을 임베딩하여
검색 키로 추가 등록하면, 사용자 query (자연 질문) 와 임베딩 key (가상 질문)
가 같은 의미 공간에서 거리가 가까워져 검색 정밀도가 향상된다 (proposition /
question-based / multi-vector retrieval 패턴).

설계 원칙
---------
- **문서 단위 1회 호출**: 256K 컨텍스트 LLM 가정. 모든 섹션을 한 번에 처리하여
  cross-section 의미 통합 + 비용 절감 (R2 ``llm_body_extractor`` 와 동일 패턴).
- **자연 질의 톤 강제**: 사용자가 실제로 검색창에 칠 법한 문장. 키워드 나열,
  답변, 명령형 모두 금지.
- **섹션별 매핑 보존**: 결과는 ``section_index`` → ``[질문, ...]`` 매핑.
  pipeline 이 청크/섹션과 조인하여 vector_store metadata 에 source 시그널 첨부.
- **빈 결과 안전 처리**: 본문이 짧거나 섹션이 없거나 LLM 실패 시 빈 dict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.ingestion.confluence_extractor import ExtractedDocument
from context_loop.processor.chunker import count_tokens
from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)


class InputTooLargeError(Exception):
    """문서 본문이 LLM 입력 한도를 초과해 호출 스킵/폴백이 필요할 때 raise."""


@dataclass(frozen=True)
class QuestionGenConfig:
    """가상 질문 생성기 옵션.

    Attributes:
        questions_per_section: 섹션당 생성할 질문 수 (LLM 지시). 실제 출력은
            모델 재량으로 ±1 가능.
        min_section_tokens: 이 미만 토큰 수의 섹션은 LLM 에 노출하되 질문
            추출 우선순위에서 후순위 (프롬프트에서 명시).
        max_input_tokens: 문서 본문 입력 토큰 한도. 초과 시
            ``InputTooLargeError``. R2 본문 그래프 호출과 동일하게 256K
            모델 안전 마진으로 200K 디폴트.
        max_output_tokens: LLM 응답 ``max_tokens``. 섹션 N개 × 질문 5개 ×
            ~30 토큰 = ~150·N. R2 32768 과 동일.
        temperature: 샘플링 온도. 가상 질문은 다양성이 약간 있어야 검색
            recall 이 올라가므로 0.3 정도가 적합하지만 결정성/재현성 우선
            정책으로 0.0.
        max_questions_per_doc: 문서 전체 질문 총량 상한. None 이면 무제한.
            거대 문서가 임베딩 호출을 폭증시키지 않게 가드.
    """

    questions_per_section: int = 5
    min_section_tokens: int = 100
    max_input_tokens: int = 200_000
    max_output_tokens: int = 32_768
    temperature: float = 0.0
    max_questions_per_doc: int | None = 50


@dataclass
class QuestionGenStats:
    """질문 생성 통계 (운영/디버그용)."""

    sections_total: int = 0
    sections_with_questions: int = 0
    raw_questions: int = 0
    dropped_questions: int = 0
    final_questions: int = 0
    llm_called: bool = False
    llm_failed: bool = False
    input_tokens_estimate: int = 0
    questions_by_section: dict[int, int] = field(default_factory=dict)


_SYSTEM_PROMPT = """\
당신은 사내 위키 문서를 색인하는 검색 엔지니어입니다. 사용자가 자연어로 사내
지식을 질문할 때, 그 질문이 어느 섹션에 정확히 매칭되어야 하는지를 알기 위해
각 섹션에서 답할 수 있는 **자연 질의 형태의 질문**을 미리 만들어 둡니다.

# 질문 작성 규칙

1. **자연 질의**: 실제 사용자가 채팅창에 칠 법한 자연스러운 한국어 문장.
   - 좋은 예: "AuthService 는 어떻게 토큰을 검증하나요?", "결제 API 의 응답 코드 정의는?"
   - 나쁜 예: "AuthService 토큰 검증" (키워드 나열), "토큰을 검증하라" (명령형),
     "토큰 검증 방법" (체언 종결)
2. **본문에 답이 있는 질문만**: 본문에 명시적으로 답이 있는 것만. 추론/외삽 금지.
3. **같은 의미 중복 금지**: 한 섹션 안에서 표현만 다른 같은 질문 X.
4. **다양한 각도**: 정의("X 가 무엇인가?"), 방법("X 가 어떻게 동작하나?"),
   사례("X 가 언제 사용되나?"), 차이("X 와 Y 의 차이는?"), 영향
   ("X 가 변경되면 어떤 일이 일어나나?") 등을 골고루.
5. **고유 명사 보존**: 본문에 등장한 고유 명사·약어는 그대로 사용.
6. **섹션 별 격리**: 다른 섹션 본문을 끌어와 만든 질문 금지. 각 섹션은 자기
   본문만으로 답할 수 있는 질문만 작성.
7. **짧은 섹션은 적게**: 본문이 매우 짧으면 질문 1~2개로 충분. 억지로 늘리지 말 것.

# 출력 형식 (JSON, 다른 텍스트 절대 포함 금지)

```json
{
  "sections": [
    {
      "section_index": 0,
      "section_path": "A > B",
      "questions": [
        "AuthService 는 어떻게 토큰을 검증하나요?",
        "토큰 만료 시 AuthService 의 동작은?"
      ]
    },
    {
      "section_index": 1,
      "section_path": "C",
      "questions": ["..."]
    }
  ]
}
```

섹션이 매우 적거나 본문이 비면 ``"sections": []`` 를 반환하세요.
"""


_USER_PROMPT_TEMPLATE = """\
# 문서 제목
{doc_title}

# 섹션별 본문
각 섹션이 답할 수 있는 자연 질의 최대 {n_per_section} 개를 작성하세요.

{sections_text}
"""


async def generate_questions_for_document(
    *,
    doc_title: str,
    extracted: ExtractedDocument,
    llm_client: LLMClient,
    config: QuestionGenConfig | None = None,
) -> tuple[dict[int, list[str]], QuestionGenStats]:
    """문서를 1회 LLM 호출로 처리하여 섹션별 가상 질문 매핑을 반환한다.

    Args:
        doc_title: 문서 제목.
        extracted: Confluence 추출 결과 (sections + plain_text).
        llm_client: LLM 클라이언트.
        config: 옵션. None 이면 기본값.

    Returns:
        ``({section_index: [questions]}, QuestionGenStats)``.
        섹션이 없거나 본문이 비면 빈 dict. ``section_index=-1`` 은 sections
        없는 plain 문서의 가상 질문을 가리킨다.

    Raises:
        InputTooLargeError: 문서 본문 토큰 수가 ``config.max_input_tokens`` 초과.
    """
    cfg = config or QuestionGenConfig()
    stats = QuestionGenStats()

    if not doc_title:
        return {}, stats

    sections_payload = _assemble_sections_payload(extracted, cfg)
    if not sections_payload:
        return {}, stats

    stats.sections_total = len(sections_payload)

    sections_text = _render_sections_for_prompt(sections_payload)
    input_tokens = count_tokens(sections_text) + count_tokens(doc_title)
    stats.input_tokens_estimate = input_tokens

    if input_tokens > cfg.max_input_tokens:
        raise InputTooLargeError(
            f"문서 본문 {input_tokens} 토큰 > 한도 {cfg.max_input_tokens}",
        )

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        doc_title=doc_title,
        n_per_section=cfg.questions_per_section,
        sections_text=sections_text,
    )

    stats.llm_called = True
    try:
        response = await llm_client.complete(
            user_prompt,
            system=_SYSTEM_PROMPT,
            max_tokens=cfg.max_output_tokens,
            temperature=cfg.temperature,
            reasoning_mode="off",
            purpose="question_generation",
        )
        payload = extract_json(response)
    except Exception:
        logger.warning(
            "가상 질문 생성 실패 — doc_title=%s", doc_title, exc_info=True,
        )
        stats.llm_failed = True
        return {}, stats

    if not isinstance(payload, dict):
        logger.warning(
            "가상 질문 응답이 JSON object 가 아님: %s", type(payload).__name__,
        )
        stats.llm_failed = True
        return {}, stats

    valid_section_indices = {idx for idx, _ in sections_payload}
    result: dict[int, list[str]] = {}
    seen_global: set[str] = set()  # 문서 전체 중복 제거 (서로 다른 섹션이 동일 질문)
    total_emitted = 0
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        return {}, stats

    for sec in raw_sections:
        if not isinstance(sec, dict):
            continue
        try:
            section_index = int(sec.get("section_index"))
        except (TypeError, ValueError):
            stats.dropped_questions += len(sec.get("questions", []) or [])
            continue
        if section_index not in valid_section_indices:
            stats.dropped_questions += len(sec.get("questions", []) or [])
            continue
        raw_questions = sec.get("questions") or []
        if not isinstance(raw_questions, list):
            continue
        stats.raw_questions += len(raw_questions)

        seen_local: set[str] = set()
        questions: list[str] = []
        for q in raw_questions:
            if not isinstance(q, str):
                stats.dropped_questions += 1
                continue
            text = q.strip()
            if not text or len(text) < 4:  # 너무 짧은 토큰 답변 등은 드롭
                stats.dropped_questions += 1
                continue
            key = text.lower()
            if key in seen_local or key in seen_global:
                stats.dropped_questions += 1
                continue
            seen_local.add(key)
            seen_global.add(key)
            questions.append(text)
            total_emitted += 1
            if (
                cfg.max_questions_per_doc is not None
                and total_emitted >= cfg.max_questions_per_doc
            ):
                break
        if questions:
            result[section_index] = questions
            stats.sections_with_questions += 1
            stats.questions_by_section[section_index] = len(questions)
        if (
            cfg.max_questions_per_doc is not None
            and total_emitted >= cfg.max_questions_per_doc
        ):
            break

    stats.final_questions = total_emitted
    return result, stats


# ---------------------------------------------------------------------------
# 내부
# ---------------------------------------------------------------------------


def _assemble_sections_payload(
    extracted: ExtractedDocument, cfg: QuestionGenConfig,
) -> list[tuple[int, str]]:
    """LLM 입력으로 노출할 (section_index, 렌더링 본문) 목록.

    sections 가 있으면 트리 순서대로 각 섹션 (헤딩 + md_content). sections 가
    비면 ``-1`` 가상 인덱스로 plain_text 전체를 1 섹션으로 노출.

    너무 짧은 (token < min_section_tokens) 섹션도 포함하되, 프롬프트에서
    "짧으면 적게" 지시로 처리.
    """
    if extracted.sections:
        out: list[tuple[int, str]] = []
        for idx, section in enumerate(extracted.sections):
            heading_line = "#" * max(section.level, 1) + " " + section.title
            body = section.md_content.strip()
            text = heading_line + "\n\n" + body if body else heading_line
            out.append((idx, text))
        return out

    plain = (extracted.plain_text or "").strip()
    if not plain:
        return []
    return [(-1, plain)]


def _render_sections_for_prompt(payload: list[tuple[int, str]]) -> str:
    """섹션 목록을 LLM 프롬프트용 텍스트로 렌더링한다."""
    chunks: list[str] = []
    for idx, text in payload:
        chunks.append(f"--- section_index={idx} ---\n{text}")
    return "\n\n".join(chunks)
