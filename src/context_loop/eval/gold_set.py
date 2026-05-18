"""골드셋 데이터 모델 + 입출력.

YAML 기반의 단순 포맷 — 외부 라이브러리 의존 없이 yaml 만 사용한다.

포맷::

    version: 1
    items:
      - id: q001
        query: "VPC quota 검증 로직이 어디 있나요?"
        relevant_doc_ids: [142, 89]
        relevant_graph_entities:        # (선택) 그래프 채점용 (name, type) 페어
          - name: "인증 서비스"
            type: "system"
            aliases: ["Auth Service", "인증서비스"]   # (선택, 2차 — 강건 매칭용)
            description: "사내 인증 게이트웨이"        # (선택, 2차)
            description_embedding: [0.01, -0.02, ...] # (선택, 2차 — 생성 시 1회 계산)
        relevant_graph_relations:        # (선택, 2차 — 관계 채점 활성화 시)
          - source_name: "인증 서비스"
            target_name: "결제 서비스"
            relation_type: "depends_on"
            description: "..."
            description_embedding: [...]
        source_type: "confluence_mcp"    # (선택) 출처 source_type
        source_document_id: 142          # (선택) 출처 문서 ID
        source_text_anchor: "..."        # (선택) 본문 prefix 인용 — chunk_id 대체
        source_section_path: "..."       # (선택) 디버깅용
        difficulty: easy | medium | hard # (선택)
        synthesized: true                # (선택) LLM 합성 여부

backward-compat: 옛 YAML 의 ``source_chunk_id`` 키는 로드 시 보존되지만
신규 생성에서는 emit 되지 않는다 (D-6). 채점 키로 사용 금지.

2차 (그래프 인덱싱 강건성): ``GraphEntityRef`` 에 ``aliases`` /
``description`` / ``description_embedding`` 필드를 추가했다. 옛 YAML 은
이 필드가 없어도 기본값 (빈 리스트 / 빈 문자열 / ``None``) 으로 로드된다.
``GraphRelationRef`` 와 ``GoldItem.relevant_graph_relations`` 는 관계 채점
(``--score-relations``) 용 옵셔널 필드.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class GraphEntityRef:
    """그래프 채점·디버그용 엔티티 참조.

    채점은 4단계 cascade 매칭 (``eval.graph_match``) 으로 판정된다 —
    exact → alias → normalize → description embedding cosine. 옛 YAML 의
    ``{name, type}`` 만 가진 항목도 자연스럽게 동작한다 (T1 + T3 만 발동).

    Attributes:
        name: 엔티티 이름. 케이스 무시 비교.
        type: 엔티티 타입. T1~T3 에서는 정확 일치, T4 (embedding) 에서는
            무시.
        aliases: 동의어 / 다른 표기. T2 단계에서 OR 매칭에 사용. 빈
            리스트면 T2 skip.
        description: 자연어 evidence. T4 단계의 임베딩 소스. 빈 문자열이면
            T4 skip.
        description_embedding: ``description`` 의 임베딩. 생성 시 1회
            계산되어 골드셋에 박힌다 — 평가 시 재계산 안 함 (재현성).
            ``None`` 이면 평가 시 lazy 계산 + LRU 캐시.
    """

    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    description_embedding: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.description:
            out["description"] = self.description
        if self.description_embedding:
            out["description_embedding"] = list(self.description_embedding)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GraphEntityRef:
        raw_aliases = d.get("aliases") or []
        aliases: list[str] = [str(a) for a in raw_aliases if isinstance(a, str)]
        raw_emb = d.get("description_embedding")
        embedding: list[float] | None
        if isinstance(raw_emb, list) and raw_emb:
            embedding = [float(x) for x in raw_emb]
        else:
            embedding = None
        return cls(
            name=str(d.get("name", "")),
            type=str(d.get("type", "")),
            aliases=aliases,
            description=str(d.get("description", "")),
            description_embedding=embedding,
        )


@dataclass
class GraphRelationRef:
    """그래프 관계(엣지) 채점용 참조.

    관계 채점 활성화 (``--score-relations``) 시에만 사용된다. 채점 키는
    ``(source_name.lower(), target_name.lower(), relation_type)`` 의
    3-tuple. ``relation_type`` 명 변경에 대비해 ``description`` 임베딩
    cosine 으로 fallback 매칭한다.

    Attributes:
        source_name: 시작 엔티티 이름.
        target_name: 도착 엔티티 이름.
        relation_type: 관계 타입 (예: ``"depends_on"``).
        description: 자연어 관계 설명. T4 embedding 소스.
        description_embedding: ``description`` 의 임베딩 (생성 시 1회).
    """

    source_name: str
    target_name: str
    relation_type: str
    description: str = ""
    description_embedding: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source_name": self.source_name,
            "target_name": self.target_name,
            "relation_type": self.relation_type,
        }
        if self.description:
            out["description"] = self.description
        if self.description_embedding:
            out["description_embedding"] = list(self.description_embedding)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GraphRelationRef:
        raw_emb = d.get("description_embedding")
        embedding: list[float] | None
        if isinstance(raw_emb, list) and raw_emb:
            embedding = [float(x) for x in raw_emb]
        else:
            embedding = None
        return cls(
            source_name=str(d.get("source_name", "")),
            target_name=str(d.get("target_name", "")),
            relation_type=str(d.get("relation_type", "")),
            description=str(d.get("description", "")),
            description_embedding=embedding,
        )


@dataclass
class GoldItem:
    """골드셋 단일 항목.

    ``relevant_doc_ids`` 와 ``relevant_graph_entities`` 중 최소 하나는 비어
    있지 않아야 한다. 둘 다 있는 hybrid 질문도 허용된다.

    ``relevant_graph_relations`` 는 관계 채점 활성화 시에만 사용되는
    옵셔널 필드 (2차 — 그래프 인덱싱 강건성).
    """

    id: str
    query: str
    relevant_doc_ids: list[int] = field(default_factory=list)
    relevant_graph_entities: list[GraphEntityRef] = field(default_factory=list)
    relevant_graph_relations: list[GraphRelationRef] = field(default_factory=list)
    source_type: str = ""
    source_document_id: int | None = None
    source_text_anchor: str | None = None
    source_section_path: str = ""
    difficulty: str = ""
    synthesized: bool = False
    notes: str = ""
    # DEPRECATED: 기존 YAML 로드 호환용. 신규 생성에서는 emit 되지 않으며
    # 채점·정렬 키로 사용 금지. 본문 lookup 의 2순위 fallback 으로만 쓰인다.
    source_chunk_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "query": self.query,
            "relevant_doc_ids": list(self.relevant_doc_ids),
        }
        if self.relevant_graph_entities:
            out["relevant_graph_entities"] = [
                e.to_dict() for e in self.relevant_graph_entities
            ]
        if self.relevant_graph_relations:
            out["relevant_graph_relations"] = [
                r.to_dict() for r in self.relevant_graph_relations
            ]
        if self.source_type:
            out["source_type"] = self.source_type
        if self.source_document_id is not None:
            out["source_document_id"] = self.source_document_id
        if self.source_text_anchor:
            out["source_text_anchor"] = self.source_text_anchor
        if self.source_section_path:
            out["source_section_path"] = self.source_section_path
        if self.difficulty:
            out["difficulty"] = self.difficulty
        if self.synthesized:
            out["synthesized"] = True
        if self.notes:
            out["notes"] = self.notes
        if self.source_chunk_id:
            out["source_chunk_id"] = self.source_chunk_id
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldItem:
        raw_entities = d.get("relevant_graph_entities") or []
        entities: list[GraphEntityRef] = []
        for e in raw_entities:
            if isinstance(e, dict):
                entities.append(GraphEntityRef.from_dict(e))
        raw_relations = d.get("relevant_graph_relations") or []
        relations: list[GraphRelationRef] = []
        for r in raw_relations:
            if isinstance(r, dict):
                relations.append(GraphRelationRef.from_dict(r))
        return cls(
            id=str(d["id"]),
            query=str(d["query"]),
            relevant_doc_ids=[int(x) for x in d.get("relevant_doc_ids", [])],
            relevant_graph_entities=entities,
            relevant_graph_relations=relations,
            source_type=str(d.get("source_type", "")),
            source_document_id=(
                int(d["source_document_id"])
                if d.get("source_document_id") is not None
                else None
            ),
            source_text_anchor=(
                str(d["source_text_anchor"])
                if d.get("source_text_anchor")
                else None
            ),
            source_section_path=str(d.get("source_section_path", "")),
            difficulty=str(d.get("difficulty", "")),
            synthesized=bool(d.get("synthesized", False)),
            notes=str(d.get("notes", "")),
            source_chunk_id=d.get("source_chunk_id") or None,
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
