"""골드셋 데이터 모델 + 입출력.

YAML 기반의 단순 포맷 — 외부 라이브러리 의존 없이 yaml 만 사용한다.

포맷::

    version: 1
    items:
      - id: q001
        query: "VPC quota 검증 로직이 어디 있나요?"
        relevant_doc_ids: [142, 89]
        relevant_doc_groups:            # (선택, R3) 정답 문서 동치 집합
          - [142, 89]                   #   inner=OR(아무거나 1개면 그룹 만족),
          - [201]                       #   outer=AND(모든 그룹이 만족돼야 완전 정답)
        cross_document: true            # (선택, R2) cross-document 질의 식별 플래그
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

3차 (source-grounded — PR #79 P0): ``SupportingFact`` (원문 근거 사실) +
``GoldItem`` 의 ``reference_answer`` / ``supporting_facts`` / ``answerable`` /
``measurement_units`` / ``provenance`` 옵셔널 필드를 추가했다. 한 GoldItem 이
``measurement_units`` 에 따라 청크/문서(``doc``) · 답변(``answer``) · 그래프
(``graph``) 세 측정 단위를 동시에 서빙한다. 모두 추가만 — 옛 YAML 은 기본값
(빈 문자열 / 빈 리스트 / ``True`` / 빈 dict) 으로 로드되며 기존 채점 경로는
영향받지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# source-grounded 골드가 서빙할 수 있는 측정 단위 (PR #79). 한 GoldItem 의
# ``measurement_units`` 는 이 집합의 부분집합이어야 한다.
MEASUREMENT_UNITS: frozenset[str] = frozenset({"doc", "answer", "graph"})


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
class SupportingFact:
    """원문 근거(source-grounded) 사실 — 정답의 진실 앵커 (PR #79 P0).

    인덱스/추출기에서 역생성한 골드(self-fitting, R3)와 달리, 정답이 **원문이
    실제로 말하는 사실**(``evidence_span`` verbatim 인용)에 고정된다. 한 사실이
    세 측정 단위를 서빙한다:

    - **doc**: ``evidence_span`` 이 속한 ``source_doc_id`` 를 회수했나 (context recall).
    - **answer**: ``GoldItem.reference_answer`` 의 근거가 되는 사실.
    - **graph**: ``(entity, relation, target)`` 트리플을 그래프에서 회수했나.

    Attributes:
        entity: 사실의 주체 엔티티 이름.
        entity_type: 엔티티 타입 (예: ``"system"``). 옛 데이터 호환 위해 기본 빈
            문자열.
        relation: 관계 타입 (예: ``"depends_on"``). 그래프 단위에서만 의미.
        target: 관계 대상 엔티티 이름. 그래프 단위에서만 의미.
        evidence_span: 원문 verbatim 인용 (진실 앵커). 생성 시 원문에 substring
            으로 실제 존재하는지 검증된다 (P1 — 환각 차단).
        source_doc_id: ``evidence_span`` 이 유래한 원문 문서 ID.
        acceptable_surface_forms: 채점 시 동일 referent 로 인정되는 표기형 —
            결정론 정규화 변형 + **검증된** 동의어만 (generator 자유나열 금지,
            PR #78 ``sanitize_*`` 게이트 재사용). T2 surface 매칭에 쓰인다.
    """

    entity: str
    entity_type: str = ""
    relation: str = ""
    target: str = ""
    evidence_span: str = ""
    source_doc_id: int | None = None
    acceptable_surface_forms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"entity": self.entity}
        if self.entity_type:
            out["entity_type"] = self.entity_type
        if self.relation:
            out["relation"] = self.relation
        if self.target:
            out["target"] = self.target
        if self.evidence_span:
            out["evidence_span"] = self.evidence_span
        if self.source_doc_id is not None:
            out["source_doc_id"] = self.source_doc_id
        if self.acceptable_surface_forms:
            out["acceptable_surface_forms"] = list(self.acceptable_surface_forms)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SupportingFact:
        raw_forms = d.get("acceptable_surface_forms") or []
        forms: list[str] = [str(s) for s in raw_forms if isinstance(s, str)]
        return cls(
            entity=str(d.get("entity", "")),
            entity_type=str(d.get("entity_type", "")),
            relation=str(d.get("relation", "")),
            target=str(d.get("target", "")),
            evidence_span=str(d.get("evidence_span", "")),
            source_doc_id=(
                int(d["source_doc_id"])
                if d.get("source_doc_id") is not None
                else None
            ),
            acceptable_surface_forms=forms,
        )


@dataclass
class GoldItem:
    """골드셋 단일 항목.

    ``relevant_doc_ids`` 와 ``relevant_graph_entities`` 중 최소 하나는 비어
    있지 않아야 한다. 둘 다 있는 hybrid 질문도 허용된다.

    ``relevant_doc_groups`` 는 R3 정답 동치 집합. 의미는 "각 inner list 내부는
    OR(아무거나 1개면 그 그룹 만족), 그룹 간은 AND(모든 그룹이 만족돼야 완전
    정답)" — CNF(논리곱 표준형). 비었으면 R3 비활성 → ``relevant_doc_ids`` 기반
    평탄 채점으로 폴백. ``cross_document`` 는 R2 cross-document 질의 식별
    플래그 (분리 집계용).

    ``relevant_graph_relations`` 는 관계 채점 활성화 시에만 사용되는
    옵셔널 필드 (2차 — 그래프 인덱싱 강건성).

    source-grounded (PR #79 P0) 옵셔널 필드:
    - ``reference_answer``: 원문 근거 기준답 (answer 단위 채점).
    - ``supporting_facts``: 답에 필요한 ``SupportingFact`` 들 (graph/doc 단위
      정답키 파생의 원천).
    - ``answerable``: 완벽한 시스템도 회수 가능한 표적인지 (False 면 분모 위생
      — 6.5; 인덱싱 표적 버킷).
    - ``measurement_units``: 이 항목이 서빙하는 단위 부분집합 (``MEASUREMENT_UNITS``
      = {doc, answer, graph}). 비었으면 단위 미태깅 (레거시).
    - ``provenance``: 생성 재현성 — extraction/generator/judge/embedding 모델 ID +
      seed 기록.
    """

    id: str
    query: str
    relevant_doc_ids: list[int] = field(default_factory=list)
    # R3 — 정답 문서 동치 집합. inner=OR, outer=AND (§2.1).
    #   - []             → R3 비활성. relevant_doc_ids 기반 평탄 채점.
    #   - [[3, 5]]       → "3 또는 5 중 하나" (단일 OR 그룹) — graph 다중-doc.
    #   - [[3, 5], [9]]  → "(3 또는 5) AND (9)" — cross-doc (R2).
    relevant_doc_groups: list[list[int]] = field(default_factory=list)
    # R2 — cross-document 식별 플래그 (분리 집계용).
    cross_document: bool = False
    relevant_graph_entities: list[GraphEntityRef] = field(default_factory=list)
    relevant_graph_relations: list[GraphRelationRef] = field(default_factory=list)
    source_type: str = ""
    source_document_id: int | None = None
    source_text_anchor: str | None = None
    source_section_path: str = ""
    difficulty: str = ""
    synthesized: bool = False
    notes: str = ""
    # source-grounded (PR #79 P0) — 모두 추가만, 기존 채점 경로 무영향.
    reference_answer: str = ""
    supporting_facts: list[SupportingFact] = field(default_factory=list)
    answerable: bool = True
    measurement_units: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    # DEPRECATED: 기존 YAML 로드 호환용. 신규 생성에서는 emit 되지 않으며
    # 채점·정렬 키로 사용 금지. 본문 lookup 의 2순위 fallback 으로만 쓰인다.
    source_chunk_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "query": self.query,
            "relevant_doc_ids": list(self.relevant_doc_ids),
        }
        if self.relevant_doc_groups:
            out["relevant_doc_groups"] = [list(g) for g in self.relevant_doc_groups]
        if self.cross_document:
            out["cross_document"] = True
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
        if self.reference_answer:
            out["reference_answer"] = self.reference_answer
        if self.supporting_facts:
            out["supporting_facts"] = [f.to_dict() for f in self.supporting_facts]
        # answerable 은 기본값 True — False 일 때만 emit (분모 위생 표적).
        if not self.answerable:
            out["answerable"] = False
        if self.measurement_units:
            out["measurement_units"] = list(self.measurement_units)
        if self.provenance:
            out["provenance"] = dict(self.provenance)
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
        raw_groups = d.get("relevant_doc_groups") or []
        groups: list[list[int]] = []
        for g in raw_groups:
            if not isinstance(g, list):
                continue
            inner = [int(x) for x in g]
            # 그룹 내 중복 제거 (순서 보존)
            seen: set[int] = set()
            dedup = [x for x in inner if not (x in seen or seen.add(x))]
            if dedup:
                groups.append(dedup)
        raw_facts = d.get("supporting_facts") or []
        facts: list[SupportingFact] = [
            SupportingFact.from_dict(f) for f in raw_facts if isinstance(f, dict)
        ]
        raw_units = d.get("measurement_units") or []
        # 알 수 없는 단위는 조용히 버린다 (스키마 위생). 순서·중복 보존 정리.
        units_seen: set[str] = set()
        units: list[str] = []
        for u in raw_units:
            su = str(u)
            if su in MEASUREMENT_UNITS and su not in units_seen:
                units_seen.add(su)
                units.append(su)
        raw_provenance = d.get("provenance")
        provenance = dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
        return cls(
            id=str(d["id"]),
            query=str(d["query"]),
            relevant_doc_ids=[int(x) for x in d.get("relevant_doc_ids", [])],
            relevant_doc_groups=groups,
            cross_document=bool(d.get("cross_document", False)),
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
            reference_answer=str(d.get("reference_answer", "")),
            supporting_facts=facts,
            answerable=bool(d.get("answerable", True)),
            measurement_units=units,
            provenance=provenance,
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
