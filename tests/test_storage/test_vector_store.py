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


# ---------------------------------------------------------------------------
# R3 — list_by_document (view 필터 + 대시보드 가상 질문 표시)
# ---------------------------------------------------------------------------


def test_list_by_document_returns_all_views(store: VectorStore) -> None:
    """view 인자 없이 호출하면 해당 문서의 모든 view 엔트리를 반환한다."""
    store.add_chunks(
        chunk_ids=["a#body", "a#meta", "a#q0", "b#body"],
        embeddings=[[0.1, 0.0]] * 4,
        documents=["A", "A", "A", "B"],
        metadatas=[
            {"document_id": 1, "logical_chunk_id": "a", "view": "body"},
            {"document_id": 1, "logical_chunk_id": "a", "view": "meta"},
            {"document_id": 1, "logical_chunk_id": "a", "view": "question",
             "question_text": "A 의 동작은?"},
            {"document_id": 2, "logical_chunk_id": "b", "view": "body"},
        ],
    )

    entries = store.list_by_document(1)
    assert len(entries) == 3
    views = {e["metadata"]["view"] for e in entries}
    assert views == {"body", "meta", "question"}


def test_list_by_document_filters_by_view(store: VectorStore) -> None:
    """view='question' 필터는 가상 질문 엔트리만 반환한다."""
    store.add_chunks(
        chunk_ids=["a#body", "a#meta", "a#q0", "a#q1", "b#body"],
        embeddings=[[0.1, 0.0]] * 5,
        documents=["A"] * 5,
        metadatas=[
            {"document_id": 1, "logical_chunk_id": "a", "view": "body"},
            {"document_id": 1, "logical_chunk_id": "a", "view": "meta"},
            {"document_id": 1, "logical_chunk_id": "a", "view": "question",
             "question_text": "A 의 동작은?"},
            {"document_id": 1, "logical_chunk_id": "a", "view": "question",
             "question_text": "A 의 의존성은?"},
            {"document_id": 2, "logical_chunk_id": "b", "view": "body"},
        ],
    )

    entries = store.list_by_document(1, view="question")
    assert len(entries) == 2
    texts = {e["metadata"]["question_text"] for e in entries}
    assert texts == {"A 의 동작은?", "A 의 의존성은?"}
    # 다른 문서의 엔트리는 포함되지 않음
    for e in entries:
        assert e["metadata"]["document_id"] == 1


def test_list_by_document_empty_when_no_match(store: VectorStore) -> None:
    """매칭 엔트리가 없으면 빈 리스트를 반환한다."""
    store.add_chunks(
        chunk_ids=["a#body"],
        embeddings=[[0.1, 0.0]],
        documents=["A"],
        metadatas=[
            {"document_id": 1, "logical_chunk_id": "a", "view": "body"},
        ],
    )
    assert store.list_by_document(1, view="question") == []
    assert store.list_by_document(999) == []
