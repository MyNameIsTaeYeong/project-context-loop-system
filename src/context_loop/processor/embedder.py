"""임베딩 생성 모듈.

OpenAI 또는 Anthropic(via OpenAI 호환) API를 통해 텍스트 임베딩을 생성한다.
config.yaml의 processor.embedding_provider 설정을 따른다.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100  # 한 번에 임베딩할 최대 텍스트 수


class EmbeddingClient(ABC):
    """임베딩 클라이언트 추상 기본 클래스."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 목록의 임베딩 벡터를 반환한다.

        Args:
            texts: 임베딩할 텍스트 목록.

        Returns:
            각 텍스트에 대응하는 임베딩 벡터 목록.
        """

    async def embed_one(self, text: str) -> list[float]:
        """단일 텍스트의 임베딩 벡터를 반환한다."""
        results = await self.embed([text])
        return results[0]


class OpenAIEmbeddingClient(EmbeddingClient):
    """OpenAI Embeddings API 클라이언트.

    Args:
        api_key: OpenAI API 키.
        model: 임베딩 모델 이름.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
    ) -> None:
        from openai import AsyncOpenAI  # noqa: PLC0415
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """배치 단위로 임베딩을 생성한다."""
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            response = await self._client.embeddings.create(
                input=batch,
                model=self._model,
            )
            results.extend([item.embedding for item in response.data])
        return results


class LocalEmbeddingClient(EmbeddingClient):
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """CPU 블로킹 작업을 executor로 실행한다."""
        if not texts:
            return []
        model = self._load_model()
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None, lambda: model.encode(texts, convert_to_numpy=True).tolist()  # type: ignore[union-attr]
        )
        return embeddings  # type: ignore[return-value]
