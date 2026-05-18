"""임베딩 생성 모듈.

langchain_core.embeddings.Embeddings를 상속하여 구현한다.
엔드포인트 URL을 생성자로 주입받아 OpenAI 호환 REST API 방식으로 요청한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx
from langchain_core.embeddings import Embeddings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100  # 한 번에 임베딩할 최대 텍스트 수


class EndpointEmbeddingClient(Embeddings):
    """OpenAI 호환 엔드포인트에 REST 요청으로 임베딩을 생성하는 클라이언트.

    생성자로 엔드포인트 URL을 주입받아 httpx를 통해 직접 REST 요청을 보낸다.
    동기(embed_documents/embed_query)와 비동기(aembed_documents/aembed_query) 모두 지원한다.

    Args:
        endpoint: 임베딩 서버 엔드포인트 기본 URL (예: "http://localhost:8080/v1").
        model: 사용할 임베딩 모델 ID.
        api_key: 엔드포인트 인증 키. 불필요한 경우 빈 문자열.
        timeout: HTTP 요청 타임아웃(초). 기본 60초.
        headers: 모든 요청에 추가할 커스텀 헤더. None 또는 빈 dict이면 미사용.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._url = endpoint
        self._model = model
        self._timeout = timeout
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            self._headers.update(headers)

    def _parse_response(self, data: dict) -> list[list[float]]:
        """응답 JSON에서 임베딩 벡터 목록을 인덱스 순으로 반환한다."""
        return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록의 임베딩을 동기 방식으로 생성한다."""
        if not texts:
            return []
        results: list[list[float]] = []
        with httpx.Client(timeout=self._timeout) as client:
            for i in range(0, len(texts), _BATCH_SIZE):
                batch = texts[i : i + _BATCH_SIZE]
                response = client.post(
                    self._url,
                    json={"input": batch, "model": self._model},
                    headers=self._headers,
                )
                response.raise_for_status()
                results.extend(self._parse_response(response.json()))
        return results

    def embed_query(self, text: str) -> list[float]:
        """단일 텍스트의 임베딩을 동기 방식으로 생성한다."""
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록의 임베딩을 비동기 방식으로 생성한다."""
        if not texts:
            return []
        results: list[list[float]] = []
        total_chars = sum(len(t) for t in texts)
        batch_count = 0
        for idx, text in enumerate(texts):
            logger.info(
                "Embedding text | provider=endpoint | model=%s | "
                "idx=%d/%d | chars=%d | text=%s",
                self._model, idx, len(texts), len(text), text,
            )
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for i in range(0, len(texts), _BATCH_SIZE):
                    batch = texts[i : i + _BATCH_SIZE]
                    response = await client.post(
                        self._url,
                        json={"input": batch, "model": self._model},
                        headers=self._headers,
                    )
                    response.raise_for_status()
                    results.extend(self._parse_response(response.json()))
                    batch_count += 1
            return results
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "Embedding call | provider=endpoint | model=%s | "
                "elapsed_ms=%.1f | num_texts=%d | batches=%d | total_chars=%d",
                self._model, elapsed_ms, len(texts), batch_count, total_chars,
            )

    async def aembed_query(self, text: str) -> list[float]:
        """단일 텍스트의 임베딩을 비동기 방식으로 생성한다."""
        results = await self.aembed_documents([text])
        return results[0]


class LocalEmbeddingClient(Embeddings):
    """로컬 sentence-transformers 기반 임베딩 클라이언트.

    sentence-transformers 패키지가 설치되어 있어야 한다.

    Args:
        model: sentence-transformers 모델 이름.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model
        self._model = None

    def _load_model(self) -> object:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # noqa: PLC0415
                self._model = SentenceTransformer(self._model_name)
            except ImportError as e:
                raise ImportError(
                    "sentence-transformers가 필요합니다: pip install sentence-transformers"
                ) from e
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록의 임베딩을 생성한다."""
        if not texts:
            return []
        model = self._load_model()
        return model.encode(texts, convert_to_numpy=True).tolist()  # type: ignore[union-attr]

    def embed_query(self, text: str) -> list[float]:
        """단일 텍스트의 임베딩을 생성한다."""
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """CPU 블로킹 작업을 executor로 실행한다."""
        if not texts:
            return []
        total_chars = sum(len(t) for t in texts)
        for idx, text in enumerate(texts):
            logger.info(
                "Embedding text | provider=local | model=%s | "
                "idx=%d/%d | chars=%d | text=%s",
                self._model_name, idx, len(texts), len(text), text,
            )
        start = time.perf_counter()
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.embed_documents, texts)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "Embedding call | provider=local | model=%s | "
                "elapsed_ms=%.1f | num_texts=%d | total_chars=%d",
                self._model_name, elapsed_ms, len(texts), total_chars,
            )

    async def aembed_query(self, text: str) -> list[float]:
        results = await self.aembed_documents([text])
        return results[0]
