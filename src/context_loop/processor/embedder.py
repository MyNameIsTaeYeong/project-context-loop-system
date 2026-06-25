"""임베딩 생성 모듈.

langchain_core.embeddings.Embeddings를 상속하여 구현한다.
엔드포인트 URL을 생성자로 주입받아 OpenAI 호환 REST API 방식으로 요청한다.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

import httpx
from langchain_core.embeddings import Embeddings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100  # 한 번에 임베딩할 최대 텍스트 수
_EMBED_MAX_CONCURRENCY = 4  # 동시 in-flight HTTP 요청 상한 (전역, "한 번에 너무 많은 요청" 방지)
_EMBED_MAX_RETRIES = 5  # 429/일시적 오류 시 최대 시도 횟수
_EMBED_BACKOFF_BASE = 2.0  # 지수 백오프 밑(초)
_EMBED_BACKOFF_MAX = 60.0  # 백오프 상한(초)


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
        max_concurrency: 동시 in-flight HTTP 요청 상한. 이 클라이언트 인스턴스를 공유하는
            모든 호출자(파이프라인/엔티티/쿼리)를 가로질러 동시 요청 총수를 제한한다.
        max_retries: 429/일시적 오류 시 배치당 최대 시도 횟수.
        backoff_base: 지수 백오프 밑(초). 429 응답의 Retry-After 헤더가 있으면 그 값을 우선한다.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
        max_concurrency: int = _EMBED_MAX_CONCURRENCY,
        max_retries: int = _EMBED_MAX_RETRIES,
        backoff_base: float = _EMBED_BACKOFF_BASE,
    ) -> None:
        self._url = endpoint
        self._model = model
        self._timeout = timeout
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            self._headers.update(headers)
        self._max_concurrency = max(1, max_concurrency)
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        # asyncio.Semaphore는 이벤트 루프에 바인딩되므로 첫 비동기 호출 시 lazy 생성한다.
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        """동시성 제한용 세마포어를 lazy 생성해 반환한다 (실행 중 이벤트 루프에 바인딩)."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    def _parse_response(self, data: dict) -> list[list[float]]:
        """응답 JSON에서 임베딩 벡터 목록을 인덱스 순으로 반환한다."""
        return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """429 또는 일시적 전송 오류처럼 재시도가 의미 있는 예외인지 판별한다."""
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status == 429 or 500 <= status < 600
        return isinstance(exc, httpx.TransportError)

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        """다음 재시도까지 대기할 시간(초). Retry-After 헤더가 있으면 우선 사용한다."""
        if isinstance(exc, httpx.HTTPStatusError):
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass
        delay = min(self._backoff_base**attempt, _EMBED_BACKOFF_MAX)
        return delay + random.uniform(0, min(1.0, delay))  # noqa: S311 (보안 무관, 지터용)

    def _post_batch_sync(self, client: httpx.Client, batch: list[str]) -> list[list[float]]:
        """동기 클라이언트로 한 배치를 임베딩한다 (429/일시 오류 시 재시도+백오프)."""
        for attempt in range(self._max_retries):
            try:
                response = client.post(
                    self._url,
                    json={"input": batch, "model": self._model},
                    headers=self._headers,
                )
                response.raise_for_status()
                return self._parse_response(response.json())
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= self._max_retries or not self._is_retryable(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "임베딩 배치 실패 (시도 %d/%d), %.1f초 후 재시도",
                    attempt + 1, self._max_retries, delay, exc_info=True,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _apost_batch(self, client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        """비동기 클라이언트로 한 배치를 임베딩한다 (전역 동시성 상한 + 재시도+백오프).

        세마포어 획득을 재시도 루프 안에 둬서, 백오프 대기 중에는 슬롯을 점유하지 않는다.
        """
        for attempt in range(self._max_retries):
            try:
                async with self._get_semaphore():
                    response = await client.post(
                        self._url,
                        json={"input": batch, "model": self._model},
                        headers=self._headers,
                    )
                    response.raise_for_status()
                    return self._parse_response(response.json())
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 >= self._max_retries or not self._is_retryable(exc):
                    raise
                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "임베딩 배치 실패 (시도 %d/%d), %.1f초 후 재시도",
                    attempt + 1, self._max_retries, delay, exc_info=True,
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록의 임베딩을 동기 방식으로 생성한다."""
        if not texts:
            return []
        results: list[list[float]] = []
        with httpx.Client(timeout=self._timeout) as client:
            for i in range(0, len(texts), _BATCH_SIZE):
                batch = texts[i : i + _BATCH_SIZE]
                results.extend(self._post_batch_sync(client, batch))
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
                    results.extend(await self._apost_batch(client, batch))
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
