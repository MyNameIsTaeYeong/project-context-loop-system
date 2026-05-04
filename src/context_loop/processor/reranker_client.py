"""전용 리랭커 모델 클라이언트.

dedicated cross-encoder 리랭커 모델 서버(Cohere/Jina/TEI 호환)와 통신한다.
요청 본문: {"model": ..., "query": ..., "documents": [...]}
응답 본문은 제공자별로 다음 중 하나를 지원한다:
- Cohere/Jina: {"results": [{"index": int, "relevance_score": float}, ...]}
- TEI: [{"index": int, "score": float}, ...]
- 단순 점수 배열: {"scores": [float, ...]} 또는 [float, ...]
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class RerankerClient(ABC):
    """리랭커 클라이언트 추상 기본 클래스."""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[float]:
        """문서 리스트의 query 관련도 점수를 반환한다.

        Returns:
            documents 와 동일 길이의 점수 리스트. 인덱스 i 는 documents[i] 의 점수.
        """


class EndpointRerankerClient(RerankerClient):
    """OpenAI/Cohere/TEI 호환 자체 호스팅 리랭커 서버 클라이언트.

    Args:
        endpoint: 리랭커 API 엔드포인트 URL (POST 대상 전체 경로).
            예: "http://localhost:8080/rerank".
        model: 사용할 모델 ID.
        api_key: 엔드포인트 인증 키. 불필요한 경우 빈 문자열.
        timeout: HTTP 요청 타임아웃(초).
        headers: 모든 요청에 추가할 커스텀 헤더.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        import httpx  # noqa: PLC0415

        self._url = endpoint
        self._model = model
        self._timeout = httpx.Timeout(timeout, connect=10.0)
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            self._headers.update(headers)

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        import httpx  # noqa: PLC0415

        body = {
            "model": self._model,
            "query": query,
            "documents": documents,
        }
        total_doc_chars = sum(len(d) for d in documents)
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=body, headers=self._headers)
                resp.raise_for_status()
                data = resp.json()
            return parse_rerank_response(data, len(documents))
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "Reranker call | purpose=context_rerank | model=%s | "
                "elapsed_ms=%.1f | num_documents=%d | query_chars=%d | "
                "doc_chars=%d",
                self._model, elapsed_ms, len(documents),
                len(query), total_doc_chars,
            )


def parse_rerank_response(data: Any, n: int) -> list[float]:
    """리랭커 API 응답을 documents 순서대로 점수 리스트로 정규화한다.

    지원 형식:
    1. Cohere/Jina: ``{"results": [{"index": int, "relevance_score": float}, ...]}``
    2. TEI: ``[{"index": int, "score": float}, ...]``
    3. 단순 배열(입력 순서 보존): ``{"scores": [float, ...]}`` 또는 ``[float, ...]``

    Args:
        data: HTTP 응답 JSON.
        n: 입력 documents 의 개수. 응답에 누락된 인덱스는 0.0 으로 채운다.
    """
    if isinstance(data, dict):
        if "results" in data:
            return _scores_from_indexed(data["results"], n)
        if "scores" in data and isinstance(data["scores"], list):
            return _scores_from_ordered(data["scores"], n)
        raise ValueError(
            f"리랭커 응답 형식을 인식할 수 없습니다: keys={list(data.keys())}"
        )

    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return _scores_from_indexed(data, n)
        return _scores_from_ordered(data, n)

    raise ValueError(f"리랭커 응답 형식을 인식할 수 없습니다: {type(data).__name__}")


def _scores_from_indexed(items: Any, n: int) -> list[float]:
    """``[{"index": int, "score"|"relevance_score": float}, ...]`` 형식을 파싱."""
    if not isinstance(items, list):
        raise ValueError("리랭커 응답의 results 가 리스트가 아닙니다")
    scores = [0.0] * n
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < n):
            continue
        score = item.get("relevance_score", item.get("score"))
        if score is None:
            continue
        scores[idx] = float(score)
    return scores


def _scores_from_ordered(values: Any, n: int) -> list[float]:
    """``[float, ...]`` 형식(입력 순서 보존)을 파싱."""
    if not isinstance(values, list):
        raise ValueError("리랭커 응답의 점수가 리스트가 아닙니다")
    scores = [0.0] * n
    for i in range(min(n, len(values))):
        scores[i] = float(values[i])
    return scores
