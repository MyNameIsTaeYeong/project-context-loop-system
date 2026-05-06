"""골드셋 직렬화/역직렬화 테스트."""

from __future__ import annotations

from pathlib import Path

from context_loop.eval.gold_set import GoldItem, GoldSet, load_gold_set, save_gold_set


def test_gold_item_to_from_dict_roundtrip() -> None:
    item = GoldItem(
        id="q001",
        query="VPC quota 검증 로직?",
        relevant_doc_ids=[42, 89],
        source_chunk_id="abc-123",
        source_document_id=42,
        source_section_path="limits.py > QuotaChecker",
        difficulty="medium",
        synthesized=True,
        notes="합성",
    )
    d = item.to_dict()
    rehydrated = GoldItem.from_dict(d)
    assert rehydrated == item


def test_gold_item_to_dict_omits_empty() -> None:
    """비어 있는 선택 필드는 출력 dict 에서 빠진다 (YAML 가독성)."""
    item = GoldItem(id="q1", query="?", relevant_doc_ids=[1])
    d = item.to_dict()
    assert "source_chunk_id" not in d
    assert "difficulty" not in d
    assert "synthesized" not in d
    assert "notes" not in d
    assert d == {"id": "q1", "query": "?", "relevant_doc_ids": [1]}


def test_gold_item_from_dict_handles_missing() -> None:
    """필수 키만 있어도 파싱된다."""
    item = GoldItem.from_dict({"id": "q1", "query": "?", "relevant_doc_ids": [1]})
    assert item.id == "q1"
    assert item.relevant_doc_ids == [1]
    assert item.synthesized is False


def test_gold_item_from_dict_coerces_int_ids() -> None:
    """relevant_doc_ids 가 문자열로 와도 int 로 변환된다."""
    item = GoldItem.from_dict({"id": "q1", "query": "?", "relevant_doc_ids": ["42", "89"]})
    assert item.relevant_doc_ids == [42, 89]


def test_save_load_roundtrip(tmp_path: Path) -> None:
    gold = GoldSet(
        version=1,
        items=[
            GoldItem(id="q1", query="첫 질의", relevant_doc_ids=[1, 2], synthesized=True),
            GoldItem(id="q2", query="두 번째", relevant_doc_ids=[3]),
        ],
        metadata={"seed": 42, "n_chunks_sampled": 30},
    )
    path = tmp_path / "gold.yaml"
    save_gold_set(gold, path)
    assert path.exists()

    loaded = load_gold_set(path)
    assert loaded.version == 1
    assert len(loaded.items) == 2
    assert loaded.items[0].query == "첫 질의"
    assert loaded.items[0].synthesized is True
    assert loaded.metadata["seed"] == 42


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    """저장 경로의 상위 디렉토리가 없으면 생성된다."""
    path = tmp_path / "deep" / "nested" / "gold.yaml"
    save_gold_set(GoldSet(items=[GoldItem(id="q1", query="?", relevant_doc_ids=[1])]), path)
    assert path.exists()


def test_load_empty_yaml(tmp_path: Path) -> None:
    """빈 YAML 도 안전하게 로드된다 (빈 골드셋)."""
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    loaded = load_gold_set(path)
    assert loaded.items == []
    assert loaded.version == 1
