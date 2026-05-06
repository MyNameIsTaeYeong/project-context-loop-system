"""골드셋 데이터 모델 + 입출력.

YAML 기반의 단순 포맷 — 외부 라이브러리 의존 없이 yaml 만 사용한다.

포맷::

    version: 1
    items:
      - id: q001
        query: "VPC quota 검증 로직이 어디 있나요?"
        relevant_doc_ids: [142, 89]
        source_chunk_id: "<uuid>"        # (선택) 생성 출처 청크 ID
        source_section_path: "..."       # (선택) 디버깅용
        difficulty: easy | medium | hard # (선택)
        synthesized: true                # (선택) LLM 합성 여부
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class GoldItem:
    """골드셋 단일 항목."""

    id: str
    query: str
    relevant_doc_ids: list[int]
    source_chunk_id: str | None = None
    source_section_path: str = ""
    source_document_id: int | None = None
    difficulty: str = ""
    synthesized: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "query": self.query,
            "relevant_doc_ids": list(self.relevant_doc_ids),
        }
        if self.source_chunk_id:
            out["source_chunk_id"] = self.source_chunk_id
        if self.source_document_id is not None:
            out["source_document_id"] = self.source_document_id
        if self.source_section_path:
            out["source_section_path"] = self.source_section_path
        if self.difficulty:
            out["difficulty"] = self.difficulty
        if self.synthesized:
            out["synthesized"] = True
        if self.notes:
            out["notes"] = self.notes
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldItem:
        return cls(
            id=str(d["id"]),
            query=str(d["query"]),
            relevant_doc_ids=[int(x) for x in d.get("relevant_doc_ids", [])],
            source_chunk_id=d.get("source_chunk_id") or None,
            source_document_id=(
                int(d["source_document_id"])
                if d.get("source_document_id") is not None
                else None
            ),
            source_section_path=str(d.get("source_section_path", "")),
            difficulty=str(d.get("difficulty", "")),
            synthesized=bool(d.get("synthesized", False)),
            notes=str(d.get("notes", "")),
        )


@dataclass
class GoldSet:
    """골드셋 전체. 메타데이터 + 항목 리스트."""

    version: int = 1
    items: list[GoldItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "metadata": self.metadata,
            "items": [it.to_dict() for it in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldSet:
        return cls(
            version=int(d.get("version", 1)),
            metadata=dict(d.get("metadata") or {}),
            items=[GoldItem.from_dict(x) for x in d.get("items", [])],
        )


def save_gold_set(gold: GoldSet, path: Path) -> None:
    """골드셋을 YAML 로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            gold.to_dict(),
            f,
            allow_unicode=True,
            sort_keys=False,
        )


def load_gold_set(path: Path) -> GoldSet:
    """YAML 골드셋을 로드한다."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return GoldSet.from_dict(data)
