"""ExtractionUnit 본문 → LLM 기반 의미 관계 추출기.

결정론 추출기(``body_extractor``)가 잡지 못하는 도메인 의미 관계
(``depends_on`` / ``implements`` / ``calls`` 등)를 LLM 호출로 보완한다.

설계 원칙
---------
- **고정 어휘**: 엔티티/관계 타입을 프롬프트로 강제하고, 출력에서 어휘 외 항목은
  드롭한다. 그래프 스키마가 무한 확장되는 것을 방지한다.
- **단위 격리**: 한 unit 의 LLM 응답이 실패하거나 파싱 안 돼도 다른 unit
  처리는 계속한다. 파이프라인 회복력 우선.
- **비용 게이팅**: 짧은 unit (예: 머리말만 있는 응축 결과) 이나 거대 섹션
  분할의 중복 part 는 LLM 호출에서 제외한다.
- **결정론 추출기와 병합**: 같은 ``GraphData`` 형식을 반환하므로 GraphStore
  의 정규 노드 병합으로 자연스럽게 합쳐진다 (``link_graph_builder`` 와
  ``body_extractor`` 가 같은 document 노드로 수렴하는 것과 동일).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from context_loop.processor.extraction_unit import ExtractionUnit
from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)


_DEFAULT_ENTITY_TYPES: tuple[str, ...] = (
    "system",      # 외부에서 보이는 서비스 (예: "Auth Service")
    "module",      # 시스템 내부 컴포넌트 (예: "Token Validator")
    "api",         # HTTP/RPC 엔드포인트 (예: "POST /v1/payments")
    "concept",     # 추상 개념/표준 (예: "OAuth2")
    "policy",      # 정책/규칙
    "person",      # 사람
    "team",        # 팀/조직
)

_DEFAULT_RELATION_TYPES: tuple[str, ...] = (
    "depends_on",   # A 가 B 에 의존
    "implements",   # A 가 B 표준/인터페이스 구현
    "calls",        # A 가 B 를 호출
    "owned_by",     # A 의 소유자가 B
    "supersedes",   # A 가 B 를 대체
    "has_part",     # A 가 B 를 포함
    "uses",         # A 가 B 를 사용
    "provides",     # A 가 B 를 제공
    "documented_in",  # A 가 B 에 문서화
)


@dataclass(frozen=True)
class LLMBodyExtractionConfig:
    """LLM 본문 추출기 옵션.

    Attributes:
        allowed_entity_types: LLM 출력에서 허용할 entity_type 목록.
        allowed_relation_types: LLM 출력에서 허용할 relation_type 목록.
        min_unit_tokens: 이 미만 토큰 수의 unit 은 LLM 호출 스킵
            (응축된 미니 섹션은 의미 관계가 거의 없음).
        skip_split_overlap_parts: 거대 섹션 분할(split_total>1) 의
            ``split_part > 0`` 인 part 는 스킵 (overlap 로 첫 part 와 중복
            추출되어 비용 낭비).
        max_units_per_doc: 문서당 처리할 최대 unit 수. None=무제한.
        max_concurrency: 동일 문서 내 unit LLM 호출의 최대 동시 실행 수.
        max_tokens: LLM 응답 max_tokens.
        temperature: 샘플링 온도 (0.0=결정적).
    """

    allowed_entity_types: tuple[str, ...] = _DEFAULT_ENTITY_TYPES
    allowed_relation_types: tuple[str, ...] = _DEFAULT_RELATION_TYPES
    min_unit_tokens: int = 200
    skip_split_overlap_parts: bool = True
    max_units_per_doc: int | None = None
    max_concurrency: int = 3
    # 본문 1500 토큰 unit 에서 entities + relations JSON 응답이 1000+ 토큰
    # 으로 늘어날 수 있어 1024 는 빠듯함. 2048 로 두면 일반적 응답 안정.
    max_tokens: int = 2048
    temperature: float = 0.0


@dataclass
class LLMBodyExtractionStats:
    """추출 결과 통계 (운영/디버그용)."""

    units_total: int = 0
    units_skipped_short: int = 0
    units_skipped_overlap: int = 0
    units_called: int = 0
    units_failed: int = 0
    raw_entities: int = 0
    raw_relations: int = 0
    dropped_entities: int = 0  # 어휘 외 타입으로 드롭된 수
    dropped_relations: int = 0  # 어휘 외 또는 끝점 누락으로 드롭된 수
    final_entities: int = 0
    final_relations: int = 0


_SYSTEM_PROMPT_TEMPLATE = """\
당신은 기술 문서에서 도메인 엔티티와 관계를 추출하는 전문가입니다.

# 엔티티 타입 (이 목록만 사용)
{entity_types}

# 관계 타입 (이 목록만 사용)
{relation_types}

# 추출 규칙
- 본문에 명시적으로 등장하는 관계만 추출하세요. 추론하지 마세요.
- 같은 개념은 같은 이름을 사용하세요 (약어보다 풀네임 우선).
- 본문에 언급되지 않은 관계 끝점(source/target)을 만들지 마세요.
- 이름은 본문에 등장한 표기를 그대로 사용하세요.
- 위 어휘에 없는 entity_type / relation_type 은 절대 사용하지 마세요.
- 명확한 시그널이 없으면 빈 배열을 반환하세요.

# 출력 형식 (JSON, 다른 텍스트 절대 포함 금지)
```json
{{
  "entities": [
    {{"name": "Auth Service", "type": "system", "description": "사용자 인증 담당"}}
  ],
  "relations": [
    {{"source": "Auth Service", "target": "Token Validator", "type": "depends_on"}}
  ]
}}
```
"""

_USER_PROMPT_TEMPLATE = """\
# 문서 제목
{doc_title}

# 본문
{body}
"""


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


async def extract_llm_body_graph(
    units: list[ExtractionUnit],
    *,
    doc_title: str,
    llm_client: LLMClient,
    config: LLMBodyExtractionConfig | None = None,
) -> tuple[GraphData, LLMBodyExtractionStats]:
    """ExtractionUnit 마다 LLM 호출로 도메인 의미 관계를 추출하고 합친다.

    Args:
        units: 같은 문서의 ExtractionUnit 목록.
        doc_title: 문서 제목 (프롬프트 메타 + 빈 입력 가드).
        llm_client: LLM 클라이언트.
        config: 추출 옵션. None 이면 기본값.

    Returns:
        ``(GraphData, LLMBodyExtractionStats)`` 튜플. 추출된 의미 관계가 없거나
        모든 unit 이 게이트로 걸러지면 빈 그래프를 반환한다.
    """
    cfg = config or LLMBodyExtractionConfig()
    stats = LLMBodyExtractionStats(units_total=len(units))

    if not doc_title or not units:
        return GraphData(), stats

    targets = _gate_units(units, cfg, stats)
    if not targets:
        return GraphData(), stats

    sem = asyncio.Semaphore(max(1, cfg.max_concurrency))

    async def run(unit: ExtractionUnit) -> tuple[ExtractionUnit, dict[str, Any] | None]:
        async with sem:
            try:
                payload = await _call_llm(unit, doc_title, llm_client, cfg)
                return unit, payload
            except Exception:
                logger.warning(
                    "LLM 본문 추출 실패 — unit_id=%s", unit.unit_id, exc_info=True,
                )
                return unit, None

    results = await asyncio.gather(*[run(u) for u in targets])

    # 엔티티: (name_lower, type) → Entity
    entities: dict[tuple[str, str], Entity] = {}
    # 관계: (source_lower, target_lower, type) → Relation (link_graph_builder 패턴)
    relations: dict[tuple[str, str, str], Relation] = {}

    allowed_etypes = set(cfg.allowed_entity_types)
    allowed_rtypes = set(cfg.allowed_relation_types)

    for unit, payload in results:
        if payload is None:
            stats.units_failed += 1
            continue
        stats.units_called += 1

        raw_entities = payload.get("entities") or []
        raw_relations = payload.get("relations") or []
        stats.raw_entities += len(raw_entities)
        stats.raw_relations += len(raw_relations)

        # 엔티티 검증/등록 — 어휘 통과한 이름만 같은 unit 관계의 끝점으로 인정
        unit_valid_entity_names: set[str] = set()
        for ent in raw_entities:
            if not isinstance(ent, dict):
                stats.dropped_entities += 1
                continue
            name = str(ent.get("name", "")).strip()
            etype = str(ent.get("type", "")).strip()
            if not name or etype not in allowed_etypes:
                stats.dropped_entities += 1
                continue
            description = str(ent.get("description", "")).strip()
            key = (name.lower(), etype)
            if key not in entities:
                entities[key] = Entity(
                    name=name, entity_type=etype, description=description,
                )
            unit_valid_entity_names.add(name.lower())

        # 관계 검증/등록 (끝점이 같은 unit 에서 어휘를 통과한 entities 에 있어야 함)
        section_label = " > ".join(unit.section_path)

        for rel in raw_relations:
            if not isinstance(rel, dict):
                stats.dropped_relations += 1
                continue
            src = str(rel.get("source", "")).strip()
            tgt = str(rel.get("target", "")).strip()
            rtype = str(rel.get("type", "")).strip()
            if (
                not src or not tgt or rtype not in allowed_rtypes
                or src.lower() not in unit_valid_entity_names
                or tgt.lower() not in unit_valid_entity_names
                or src.lower() == tgt.lower()
            ):
                stats.dropped_relations += 1
                continue

            rel_key = (src.lower(), tgt.lower(), rtype)
            if rel_key not in relations:
                # 정규 표기: 등록된 entity 의 표기를 사용 (대소문자 일관성)
                src_canon = _canonical_name(entities, src, src.lower())
                tgt_canon = _canonical_name(entities, tgt, tgt.lower())
                relations[rel_key] = Relation(
                    source=src_canon,
                    target=tgt_canon,
                    relation_type=rtype,
                    label=section_label,
                )

    stats.final_entities = len(entities)
    stats.final_relations = len(relations)

    if not relations:
        return GraphData(), stats

    return GraphData(
        entities=list(entities.values()),
        relations=list(relations.values()),
    ), stats


# ---------------------------------------------------------------------------
# 내부
# ---------------------------------------------------------------------------


def _gate_units(
    units: list[ExtractionUnit],
    cfg: LLMBodyExtractionConfig,
    stats: LLMBodyExtractionStats,
) -> list[ExtractionUnit]:
    """비용 게이트로 LLM 호출 대상 unit 만 추린다."""
    targets: list[ExtractionUnit] = []
    for unit in units:
        if unit.token_count < cfg.min_unit_tokens:
            stats.units_skipped_short += 1
            continue
        if cfg.skip_split_overlap_parts and unit.split_total > 1 and unit.split_part > 0:
            stats.units_skipped_overlap += 1
            continue
        targets.append(unit)

    if cfg.max_units_per_doc is not None and len(targets) > cfg.max_units_per_doc:
        targets = targets[: cfg.max_units_per_doc]
    return targets


async def _call_llm(
    unit: ExtractionUnit,
    doc_title: str,
    llm_client: LLMClient,
    cfg: LLMBodyExtractionConfig,
) -> dict[str, Any]:
    """unit 하나에 대해 LLM 호출 → JSON 파싱.

    ``reasoning_mode="off"`` 는 Qwen3/DeepSeek 등 reasoning 모델의 사고 모드를
    비활성화한다. 사고 모드가 켜진 채로 JSON 추출 프롬프트를 받으면 모델이
    ``max_tokens`` 예산을 사고에 모두 소진하고 실제 JSON 답변이 비거나 잘리는
    문제를 방지한다 (``graph_search_planner`` 와 같은 처방). 모델별 실제
    페이로드는 ``llm.reasoning_profiles`` 설정에서 매핑하며, 미지원 클라이언트
    (Anthropic, OpenAI) 는 인자를 무시한다.
    """
    system = _SYSTEM_PROMPT_TEMPLATE.format(
        entity_types=_format_vocab(cfg.allowed_entity_types),
        relation_types=_format_vocab(cfg.allowed_relation_types),
    )
    user = _USER_PROMPT_TEMPLATE.format(doc_title=doc_title, body=unit.content)
    response = await llm_client.complete(
        user,
        system=system,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        reasoning_mode="off",
        purpose="body_extraction",
    )
    payload = extract_json(response)
    if not isinstance(payload, dict):
        raise ValueError(f"LLM 응답이 JSON object 가 아님: {type(payload).__name__}")
    return payload


def _format_vocab(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _canonical_name(
    entities: dict[tuple[str, str], Entity],
    raw_name: str,
    raw_lower: str,
) -> str:
    """등록된 entity 중 같은 이름이 있으면 그 표기를 우선 반환."""
    for (name_lower, _etype), ent in entities.items():
        if name_lower == raw_lower:
            return ent.name
    return raw_name
