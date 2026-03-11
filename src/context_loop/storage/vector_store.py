"""ChromaDB 벡터 저장소 래퍼.

로컬 임베디드 모드로 ChromaDB를 사용하여 텍스트 청크를 저장하고 검색한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "context_loop_chunks"


class VectorStore:
    """ChromaDB 기반 벡터 저장소.

    Args:
        data_dir: ChromaDB 데이터를 저장할 디렉토리.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._client: Any = None
        self._collection: Any = None

    def initialize(self) -> None:
        """ChromaDB 클라이언트와 컬렉션을 초기화한다."""
        import chromadb  # noqa: PLC0415

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._data_dir / "chromadb"))
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.debug("ChromaDB 초기화 완료: %s", self._data_dir)

    @property
    def collection(self) -> Any:
        if self._collection is None:
            raise RuntimeError("VectorStore가 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        return self._collection

    def add_chunks(
        self,
        chunk_ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """청크를 벡터 저장소에 추가한다.

        Args:
            chunk_ids: 각 청크의 고유 ID.
            embeddings: 각 청크의 임베딩 벡터.
            documents: 각 청크의 텍스트.
            metadatas: 각 청크의 메타데이터 (document_id, chunk_index 등).
        """
        if not chunk_ids:
            return
        self.collection.add(
            ids=chunk_ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def delete_by_document(self, document_id: int) -> None:
        """특정 문서의 모든 청크를 삭제한다.

        Args:
            document_id: 삭제할 문서 ID.
        """
        self.collection.delete(where={"document_id": document_id})

    def search(
        self,
        query_embedding: list[float],
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """유사도 검색을 수행하고 결과를 반환한다.

        Args:
            query_embedding: 질의 임베딩 벡터.
            n_results: 반환할 최대 결과 수.
            where: ChromaDB 메타데이터 필터 조건.

        Returns:
            결과 목록. 각 항목에 id, document, metadata, distance가 포함된다.
        """
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = self.collection.query(**kwargs)

        output: list[dict[str, Any]] = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for i, chunk_id in enumerate(ids):
            output.append({
                "id": chunk_id,
                "document": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else 1.0,
            })
        return output

    def count(self) -> int:
        """저장된 청크 수를 반환한다."""
        return self.collection.count()
