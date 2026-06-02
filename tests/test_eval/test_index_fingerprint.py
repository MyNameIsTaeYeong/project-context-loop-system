"""Phase 0 — 인덱스/코퍼스 지문(index_fingerprint) 단위 테스트.

지문이 (i) 같은 입력에 대해 결정적(bit-identical)이고 (ii) 내용이 바뀌면
달라지는지, (iii) 스토어 조회 실패 시 빈 해시로 안전 폴백하는지 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from context_loop.eval.index_fingerprint import (  # noqa: E402
    corpus_fingerprint,
    graph_store_fingerprint,
    vector_store_fingerprint,
)


class _FakeCollection:
    def __init__(self, ids: list[str], metas: list[dict[str, Any]]) -> None:
        self._ids = ids
        self._metas = metas

    def get(self, include: list[str] | None = None) -> dict[str, Any]:
        return {"ids": list(self._ids), "metadatas": list(self._metas)}


class _FakeVectorStore:
    def __init__(self, ids: list[str], metas: list[dict[str, Any]]) -> None:
        self.collection = _FakeCollection(ids, metas)

    def count(self) -> int:
        return len(self.collection._ids)


class _BrokenVectorStore:
    @property
    def collection(self) -> Any:  # pragma: no cover - 단순 raise
        raise RuntimeError("boom")

    def count(self) -> int:
        return 5


class _FakeGraphStore:
    def __init__(self, fp: dict[str, Any]) -> None:
        self._fp = fp

    def content_fingerprint(self) -> dict[str, Any]:
        return dict(self._fp)


class _FakeMetaStore:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def list_documents(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self._docs]


def _vs(ids: list[str], metas: list[dict[str, Any]]) -> _FakeVectorStore:
    return _FakeVectorStore(ids, metas)


def test_vector_fingerprint_deterministic() -> None:
    metas = [
        {"document_id": 1, "view": "body", "section_path": "a"},
        {"document_id": 2, "view": "meta", "section_path": "b"},
    ]
    a = vector_store_fingerprint(_vs(["v1", "v2"], metas))
    b = vector_store_fingerprint(_vs(["v1", "v2"], metas))
    assert a["sha256"] == b["sha256"] != ""
    assert a["n_vectors"] == 2


def test_vector_fingerprint_order_independent() -> None:
    """도착 순서가 달라도 지문은 동일해야 한다(id 정렬)."""
    m1 = {"document_id": 1, "view": "body", "section_path": "a"}
    m2 = {"document_id": 2, "view": "meta", "section_path": "b"}
    a = vector_store_fingerprint(_vs(["v1", "v2"], [m1, m2]))
    b = vector_store_fingerprint(_vs(["v2", "v1"], [m2, m1]))
    assert a["sha256"] == b["sha256"]


def test_vector_fingerprint_changes_on_add() -> None:
    base = vector_store_fingerprint(
        _vs(["v1"], [{"document_id": 1, "view": "body", "section_path": "a"}])
    )
    more = vector_store_fingerprint(
        _vs(
            ["v1", "v2"],
            [
                {"document_id": 1, "view": "body", "section_path": "a"},
                {"document_id": 2, "view": "body", "section_path": "c"},
            ],
        )
    )
    assert base["sha256"] != more["sha256"]


def test_vector_fingerprint_failure_is_safe() -> None:
    out = vector_store_fingerprint(_BrokenVectorStore())
    assert out["sha256"] == ""


def test_graph_fingerprint_delegates() -> None:
    fp = {"nodes": 3, "edges": 2, "sha256": "abc"}
    assert graph_store_fingerprint(_FakeGraphStore(fp)) == fp


async def test_corpus_fingerprint_deterministic_and_sensitive() -> None:
    docs = [
        {"id": 1, "source_type": "git_code", "content_hash": "h1"},
        {"id": 2, "source_type": "confluence_mcp", "content_hash": "h2"},
    ]
    a = await corpus_fingerprint(_FakeMetaStore(docs))
    b = await corpus_fingerprint(_FakeMetaStore(list(reversed(docs))))
    assert a["sha256"] == b["sha256"] != ""
    assert a["n_documents"] == 2

    changed = await corpus_fingerprint(
        _FakeMetaStore(
            [
                {"id": 1, "source_type": "git_code", "content_hash": "h1-CHANGED"},
                {"id": 2, "source_type": "confluence_mcp", "content_hash": "h2"},
            ]
        )
    )
    assert changed["sha256"] != a["sha256"]
