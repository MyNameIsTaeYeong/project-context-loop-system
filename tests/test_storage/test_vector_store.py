"""VectorStore 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_loop.storage.vector_store import VectorStore


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:  # type: ignore[misc]
    s = VectorStore(tmp_path)
    s.initialize()
    yield s


def test_add_and_count(store: VectorStore) -> None:
    """청크를 추가하면 count가 증가한다."""
    assert store.count() == 0
    store.add_chunks(
        chunk_ids=["c1", "c2"],
        embeddings=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
        documents=["chunk one", "chunk two"],
        metadatas=[{"document_id": 1}, {"document_id": 1}],
    )
    assert store.count() == 2


def test_search_returns_results(store: VectorStore) -> None:
    """검색 결과를 반환한다."""
    store.add_chunks(
        chunk_ids=["c1"],
        embeddings=[[1.0, 0.0, 0.0]],
        documents=["hello world"],
        metadatas=[{"document_id": 1, "chunk_index": 0}],
    )
    results = store.search([1.0, 0.0, 0.0], n_results=1)
    assert len(results) == 1
    assert results[0]["id"] == "c1"
    assert results[0]["document"] == "hello world"
    assert "distance" in results[0]


def test_delete_by_document(store: VectorStore) -> None:
    """문서 ID로 청크를 삭제한다."""
    store.add_chunks(
        chunk_ids=["c1", "c2", "c3"],
        embeddings=[[0.1, 0.2, 0.3]] * 3,
        documents=["a", "b", "c"],
        metadatas=[
            {"document_id": 1},
            {"document_id": 1},
            {"document_id": 2},
        ],
    )
    store.delete_by_document(1)
    assert store.count() == 1  # document_id=2 만 남음


def test_add_empty_does_nothing(store: VectorStore) -> None:
    """빈 목록 추가는 오류를 발생시키지 않는다."""
    store.add_chunks([], [], [], [])
    assert store.count() == 0
