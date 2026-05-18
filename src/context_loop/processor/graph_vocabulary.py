"""그래프 어휘 단일 출처 (entity_type / relation_type 정의).

여러 추출기(``link_graph_builder``, ``body_extractor``, ``llm_body_extractor``)
가 각자의 어휘를 사용하지만, ``GraphStore`` 의 정규 노드 병합 덕분에
같은 ``(name, entity_type)`` 은 같은 노드로 수렴한다. 이 모듈은 시스템
전체에서 사용되는 entity/relation 어휘를 한 곳에 정의하여 그래프 탐색
플래너 같은 소비자가 일관된 가이드를 LLM 에 제공할 수 있게 한다.

추출기 측 코드는 자체 어휘 상수를 그대로 유지하지만, 이 모듈이 그것들을
**상위집합으로 재선언** 한다. 향후 추출기들의 어휘가 확장되면 이 모듈도
함께 갱신해야 한다 (테스트가 누락을 잡는다).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VocabEntry:
    """어휘 항목 한 줄.

    Attributes:
        name: entity_type 또는 relation_type 이름.
        description: LLM 프롬프트에 노출할 한국어 설명.
        source: 어디서 추출되는지 (디버깅/문서화용).
    """

    name: str
    description: str
    source: str


# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------

ENTITY_TYPES: tuple[VocabEntry, ...] = (
    # 결정론 (link_graph_builder, body_extractor)
    VocabEntry("document", "Confluence 문서/페이지", "link_graph + body"),
    VocabEntry("person", "사용자/담당자 (ri:user 또는 LLM 추출)",
               "link_graph + llm_body"),
    VocabEntry("ticket", "Jira 이슈 (PROJ-123 등)", "link_graph + body"),
    VocabEntry("attachment", "Confluence 첨부 파일", "link_graph"),
    VocabEntry("api", "HTTP/RPC 엔드포인트 (예: POST /v1/payments)",
               "body + llm_body"),
    VocabEntry("concept", "추상 개념/표준/용어 (예: OAuth2)",
               "body + llm_body"),
    # LLM 의미 어휘 (llm_body_extractor)
    VocabEntry("system", "외부에서 보이는 서비스 (예: Auth Service)", "llm_body"),
    VocabEntry("module", "시스템 내부 컴포넌트 (예: Token Validator)",
               "llm_body"),
    VocabEntry("policy", "정책/규칙", "llm_body"),
    VocabEntry("team", "팀/조직", "llm_body"),
    # AST 코드 추출
    VocabEntry("function", "함수 심볼 (git_code 추출)", "ast_code"),
    VocabEntry("class", "클래스 심볼 (git_code 추출)", "ast_code"),
)


# ---------------------------------------------------------------------------
# Relation types
# ---------------------------------------------------------------------------

RELATION_TYPES: tuple[VocabEntry, ...] = (
    # 결정론 (link_graph_builder)
    VocabEntry("references", "문서가 다른 페이지를 참조 (Confluence link)",
               "link_graph"),
    VocabEntry("mentions_user", "문서가 사용자를 언급 (@mention)", "link_graph"),
    VocabEntry("mentions_ticket", "문서가 Jira 이슈를 언급",
               "link_graph + body"),
    VocabEntry("has_attachment", "문서에 첨부 파일이 있음", "link_graph"),
    # 결정론 (body_extractor)
    VocabEntry("mentions", "문서가 개념/엔티티를 본문에서 언급", "body"),
    VocabEntry("documents", "문서가 API 엔드포인트 등을 명세", "body"),
    VocabEntry("has_attribute", "엔티티가 속성/필드를 가짐 (표 헤더)", "body"),
    # LLM 의미 (llm_body_extractor)
    VocabEntry("depends_on", "A 가 B 에 의존 (런타임/빌드 의존성)", "llm_body"),
    VocabEntry("implements", "A 가 B 표준/인터페이스를 구현", "llm_body"),
    VocabEntry("calls", "A 가 B 를 호출 (동기/비동기)", "llm_body"),
    VocabEntry("owned_by", "A 의 소유자가 B (팀/사람)", "llm_body"),
    VocabEntry("supersedes", "A 가 B 를 대체/폐기 처리", "llm_body"),
    VocabEntry("has_part", "A 가 B 를 구성 요소로 포함", "llm_body"),
    VocabEntry("uses", "A 가 B 를 사용 (도구/라이브러리)", "llm_body"),
    VocabEntry("provides", "A 가 B 를 제공 (서비스/기능)", "llm_body"),
    VocabEntry("documented_in", "A 가 B 에 문서화", "llm_body"),
    # AST 코드 추출
    VocabEntry("import", "모듈이 다른 모듈을 import", "ast_code"),
    VocabEntry("contains", "클래스가 메서드를 포함 등", "ast_code"),
)


# ---------------------------------------------------------------------------
# 의도 → 관계 매핑 (LLM 플래너 가이드용)
# ---------------------------------------------------------------------------

INTENT_TO_RELATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("의존 관계 / depends / 무엇이 필요한가",
     ("depends_on", "uses", "calls", "has_part")),
    ("소유자 / 담당자 / 팀",
     ("owned_by", "mentions_user")),
    ("표준/구현/인터페이스",
     ("implements", "provides")),
    ("폐기/대체/마이그레이션",
     ("supersedes",)),
    ("API / 엔드포인트",
     ("documents", "calls", "provides")),
    ("관련 이슈 / 티켓",
     ("mentions_ticket",)),
    ("문서 간 참조 / 관련 문서",
     ("references", "documented_in", "mentions")),
)


# ---------------------------------------------------------------------------
# LLM 프롬프트용 포맷터
# ---------------------------------------------------------------------------


def format_entity_types_for_prompt() -> str:
    """LLM 시스템 프롬프트에 삽입할 entity_type 어휘 텍스트."""
    return "\n".join(f"- **{e.name}**: {e.description}" for e in ENTITY_TYPES)


def format_relation_types_for_prompt() -> str:
    """LLM 시스템 프롬프트에 삽입할 relation_type 어휘 텍스트."""
    return "\n".join(f"- **{r.name}**: {r.description}" for r in RELATION_TYPES)


def format_intent_mapping_for_prompt() -> str:
    """질의 의도 → 주목할 관계 매핑 가이드."""
    lines = []
    for intent, rels in INTENT_TO_RELATIONS:
        lines.append(f"- {intent} → {', '.join(rels)}")
    return "\n".join(lines)


def all_entity_type_names() -> set[str]:
    return {e.name for e in ENTITY_TYPES}


def all_relation_type_names() -> set[str]:
    return {r.name for r in RELATION_TYPES}
