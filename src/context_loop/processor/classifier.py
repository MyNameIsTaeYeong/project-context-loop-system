"""LLM 기반 문서 저장 방식 판단 (Classifier).

문서 내용을 LLM이 분석하여 최적 저장 방식을 결정한다.
- "chunk": 서술형 문서 → 텍스트 청크 + 벡터DB
- "graph": 엔티티/관계 중심 문서 → 그래프DB
- "hybrid": 서술 + 관계 정보 혼합 → 청크 + 그래프 모두

긴 문서는 시작/중간/끝 구간을 샘플링하여 전체 구조를 파악한다.
"""

from __future__ import annotations

import logging
from typing import Literal

from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)

StorageMethod = Literal["chunk", "graph", "hybrid"]

_SYSTEM_PROMPT = """You are a document classifier for a knowledge management system.
Your task is to analyze a document and decide the best storage strategy.

Storage strategies:
- "chunk": Best for narrative text, guides, manuals, meeting notes, API documentation.
  These are searched via vector similarity.
- "graph": Best for documents where entity relationships are the core value — org charts,
  architecture diagrams, dependency maps, team structures.
- "hybrid": Best for documents with both narrative sections AND significant entity
  relationships — project specs with milestones and dependencies, system docs with
  architecture AND prose explanations.

Respond ONLY with a JSON object in this exact format:
{"method": "chunk"|"graph"|"hybrid", "reason": "<one sentence explanation>"}"""

_USER_PROMPT_TEMPLATE = """Classify this document:

Title: {title}

{content_label}:
{content}"""

# 전체 문서를 볼 수 있는 최대 문자 수
_MAX_TOTAL_CHARS = 4000
# 구간별 최대 문자 수 (시작/중간/끝)
_SECTION_CHARS = 1300


async def classify_document(
    client: LLMClient,
    title: str,
    content: str,
) -> tuple[StorageMethod, str]:
    """LLM을 사용하여 문서의 최적 저장 방식을 판정한다.

    짧은 문서는 전문을 전달하고, 긴 문서는 시작/중간/끝 구간을
    샘플링하여 전체 구조를 파악한 뒤 분류한다.

    Args:
        client: LLMClient 인스턴스.
        title: 문서 제목.
        content: 문서 원본 내용.

    Returns:
        (저장 방식, 판정 이유) 튜플.
        저장 방식은 "chunk", "graph", "hybrid" 중 하나.
    """
    sampled, label = _sample_content(content)
    prompt = _USER_PROMPT_TEMPLATE.format(
        title=title,
        content_label=label,
        content=sampled,
    )

    response = await client.complete(
        prompt,
        system=_SYSTEM_PROMPT,
        max_tokens=1024,
        temperature=0.0,
    )

    try:
        data = extract_json(response)
        method = data.get("method", "chunk")
        reason = data.get("reason", "")
        if method not in ("chunk", "graph", "hybrid"):
            logger.warning("알 수 없는 저장 방식 '%s', 'chunk'로 폴백", method)
            method = "chunk"
        return method, reason  # type: ignore[return-value]
    except ValueError:
        logger.warning(
            "분류 응답 파싱 실패, 'chunk'로 폴백. 응답: %s", response[:200]
        )
        return "chunk", "파싱 실패 — 기본값 chunk 적용"


def _sample_content(content: str) -> tuple[str, str]:
    """문서 내용을 샘플링한다.

    짧은 문서(MAX_TOTAL_CHARS 이하)는 전문을 반환.
    긴 문서는 시작/중간/끝 세 구간에서 균등 샘플링.

    Returns:
        (샘플링된 텍스트, 레이블) 튜플.
        레이블은 "Content" 또는 "Content (sampled from beginning/middle/end)".
    """
    if len(content) <= _MAX_TOTAL_CHARS:
        return content, "Content"

    total_len = len(content)
    mid_start = (total_len - _SECTION_CHARS) // 2

    beginning = content[:_SECTION_CHARS]
    middle = content[mid_start:mid_start + _SECTION_CHARS]
    end = content[-_SECTION_CHARS:]

    sampled = (
        f"[Beginning]\n{beginning}\n\n"
        f"[Middle]\n{middle}\n\n"
        f"[End]\n{end}"
    )
    return sampled, f"Content (sampled from beginning/middle/end, total {total_len} chars)"
