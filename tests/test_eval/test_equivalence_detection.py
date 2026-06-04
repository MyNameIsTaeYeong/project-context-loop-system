"""Phase 3.5 — OR-동치 자동 검출(find_equivalent_documents) 단위 테스트.

검증 항목:
  (i)  동등 문서(고유사도 + answer-containment yes)는 채택된다.
  (ii) 유사도 하한 미만 문서는 후보에서 제외된다(과탐 방지).
  (iii) answer-containment 가 no 면 채택되지 않는다.
  (iv) 원 출처 문서(source_document_id)는 항상 제외된다.
  (v)  embedding_client / vector_store 가 없으면 빈 리스트(안전).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from context_loop.eval.synth import find_equivalent_documents  # noqa: E402


class _FakeEmbeddingClient:
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeVectorStore:
    """search 가 고정 결과를 반환. distance = 1 - similarity."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results

    def search(
        self, query_embedding: list[float], n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return list(self._results)


class _FakeJudge:
    """document 본문에 'ANSWER' 가 있으면 yes, 아니면 no 를 반환하는 Judge."""

    def __init__(self, yes_docs: set[str]) -> None:
        self._yes_docs = yes_docs
        self.calls = 0

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        # 프롬프트에 후보 본문이 포함되므로 마커로 yes/no 결정.
        for marker in self._yes_docs:
            if marker in prompt:
                return "yes"
        return "no"


def _result(doc_id: int, similarity: float, marker: str) -> dict[str, Any]:
    return {
        "id": f"chunk-{doc_id}",
        "document": f"본문 {marker}",
        "metadata": {"document_id": doc_id},
        "distance": 1.0 - similarity,
    }


async def test_equivalent_document_accepted() -> None:
    vs = _FakeVectorStore([_result(99, 0.9, "ANSWER")])
    judge = _FakeJudge(yes_docs={"ANSWER"})
    out = await find_equivalent_documents(
        "재시도 몇 회?", "정답 본문", source_document_id=42,
        embedding_client=_FakeEmbeddingClient(), vector_store=vs, judge=judge,
        top_m=3, min_similarity=0.6,
    )
    assert out == [99]


async def test_low_similarity_excluded() -> None:
    """유사도 0.4 < 하한 0.6 → answer-containment 호출 전에 제외."""
    vs = _FakeVectorStore([_result(99, 0.4, "ANSWER")])
    judge = _FakeJudge(yes_docs={"ANSWER"})
    out = await find_equivalent_documents(
        "q", "정답", source_document_id=42,
        embedding_client=_FakeEmbeddingClient(), vector_store=vs, judge=judge,
        top_m=3, min_similarity=0.6,
    )
    assert out == []
    assert judge.calls == 0  # 후보가 없어 LLM 호출 자체가 없어야 함


async def test_not_answerable_rejected() -> None:
    """고유사도지만 answer-containment 가 no → 채택 안 됨."""
    vs = _FakeVectorStore([_result(99, 0.9, "OTHER")])
    judge = _FakeJudge(yes_docs={"ANSWER"})  # OTHER 는 no
    out = await find_equivalent_documents(
        "q", "정답", source_document_id=42,
        embedding_client=_FakeEmbeddingClient(), vector_store=vs, judge=judge,
        top_m=3, min_similarity=0.6,
    )
    assert out == []


async def test_source_document_excluded() -> None:
    """검색 결과에 원 출처(42)가 섞여도 제외된다."""
    vs = _FakeVectorStore([
        _result(42, 0.99, "ANSWER"),
        _result(99, 0.9, "ANSWER"),
    ])
    judge = _FakeJudge(yes_docs={"ANSWER"})
    out = await find_equivalent_documents(
        "q", "정답", source_document_id=42,
        embedding_client=_FakeEmbeddingClient(), vector_store=vs, judge=judge,
        top_m=3, min_similarity=0.6,
    )
    assert out == [99]


async def test_missing_clients_safe() -> None:
    assert await find_equivalent_documents(
        "q", "정답", source_document_id=42,
        embedding_client=None, vector_store=None, judge=_FakeJudge(set()),
    ) == []
