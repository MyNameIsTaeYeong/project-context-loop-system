"""그래프 어휘 단일 출처 (entity_type / relation_type 정의).

여러 추출기(``link_graph_builder``, ``body_extractor``, ``llm_body_extractor``)
가 각자의 어휘를 사용하지만, ``GraphStore`` 의 정규 노드 병합 덕분에
같은 ``(name, entity_type)`` 은 같은 노드로 수렴한다. 이 모듈은 시스템
전체에서 사용되는 entity/relation 어휘를 한 곳에 정의하여 그래프 탐색
플래너 같은 소비자가 일관된 가이드를 LLM 에 제공할 수 있게 한다.

추출기 측 코드는 자체 어휘 상수를 그대로 유지하지만, 이 모듈이 그것들을
**상위집합으로 재선언** 한다. 향후 추출기들의 어휘가 확장되면 이 모듈도
함께 갱신해야 한다 (테스트가 누락을 잡는다).

동의어 정규화 (alias):
    과거 어휘가 의미상 겹치게 세분화되어 있었다 (``has_part`` vs
    ``contains``, ``uses`` vs ``depends_on`` 등). 노드 병합 키가
    ``(entity_name, entity_type)`` 이고 평가 T1 매칭이 relation_type 정확
    일치를 요구하므로, 동의어는 노드 분열과 매칭 실패를 유발한다.
    ``ENTITY_TYPE_ALIASES`` / ``RELATION_TYPE_ALIASES`` 가 이를 canonical
    이름으로 수렴시킨다 — 결정론 추출기는 자체 상수를 그대로 emit 하고,
    ``GraphStore.save_graph_data`` 저장 시점과 ``llm_body_extractor`` 파싱
    시점에 한 번 정규화된다. 기존 인덱스는 재인덱싱해야 canonical 어휘로
    수렴한다.
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
    VocabEntry("concept", "추상 개념/표준/용어/정책/규칙 (예: OAuth2, 보안 정책)",
               "body + llm_body"),
    # LLM 의미 어휘 (llm_body_extractor)
    VocabEntry(
        "system",
        "독립적으로 배포/운영되는 서비스·애플리케이션 (예: Auth Service). "
        "시스템 내부 부품은 module 을 사용",
        "llm_body",
    ),
    VocabEntry(
        "module",
        "시스템 내부 컴포넌트 또는 코드 파일/패키지 — 독립 서비스가 아닌 것 "
        "(예: Token Validator, user_service.py)",
        "llm_body + ast_code",
    ),
    VocabEntry("team", "팀/조직", "llm_body"),
    # AST 코드 추출
    VocabEntry("function", "함수 심볼 (git_code 추출)", "ast_code"),
    VocabEntry("class", "클래스/구조체 심볼 (Go struct 포함, git_code 추출)",
               "ast_code"),
    VocabEntry("method", "클래스/구조체에 속한 메서드 심볼 (git_code 추출)",
               "ast_code"),
    VocabEntry("interface", "Go/TypeScript/Java 인터페이스 (git_code 추출)",
               "ast_code"),
)


# ---------------------------------------------------------------------------
# Relation types
# ---------------------------------------------------------------------------

RELATION_TYPES: tuple[VocabEntry, ...] = (
    # 결정론 (link_graph_builder)
    VocabEntry("references", "문서가 다른 페이지를 참조 (Confluence link)",
               "link_graph"),
    VocabEntry("has_attachment", "문서에 첨부 파일이 있음", "link_graph"),
    # 결정론 (link_graph_builder + body_extractor)
    VocabEntry(
        "mentions",
        "문서가 개념/사람/티켓 등 엔티티를 언급 (@mention, 본문 언급 — "
        "대상 의미는 타깃 entity_type 이 담당)",
        "link_graph + body",
    ),
    VocabEntry("documents", "A(문서)가 B(API/개념 등)를 명세/문서화",
               "body + llm_body"),
    # LLM 의미 (llm_body_extractor)
    VocabEntry(
        "depends_on",
        "A 가 B 에 의존하거나 B 를 사용 (런타임/빌드 의존성, 도구/라이브러리)",
        "llm_body",
    ),
    VocabEntry("implements", "A 가 B 표준/인터페이스를 구현", "llm_body"),
    VocabEntry("calls", "A 가 B 를 호출 (동기/비동기)", "llm_body"),
    VocabEntry("owned_by", "A 의 소유자가 B (팀/사람)", "llm_body"),
    VocabEntry("supersedes", "A 가 B 를 대체/폐기 처리", "llm_body"),
    VocabEntry("provides", "A 가 B 를 제공 (서비스/기능)", "llm_body"),
    # 공용 부분-전체 (body_extractor + llm_body_extractor + ast_code)
    VocabEntry(
        "contains",
        "A 가 B 를 구성 요소/속성/멤버로 포함 (시스템→부품, 엔티티→속성, "
        "클래스→메서드)",
        "body + llm_body + ast_code",
    ),
    # AST 코드 추출
    VocabEntry("imports", "모듈이 다른 모듈을 import", "ast_code"),
)


# ---------------------------------------------------------------------------
# 의도 → 관계 매핑 (LLM 플래너 가이드용)
# ---------------------------------------------------------------------------

INTENT_TO_RELATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("의존 관계 / depends / 무엇이 필요한가",
     ("depends_on", "calls", "contains")),
    ("소유자 / 담당자 / 팀",
     ("owned_by", "mentions")),
    ("표준/구현/인터페이스",
     ("implements", "provides")),
    ("폐기/대체/마이그레이션",
     ("supersedes",)),
    ("API / 엔드포인트",
     ("documents", "calls", "provides")),
    ("관련 이슈 / 티켓",
     ("mentions",)),
    ("문서 간 참조 / 관련 문서",
     ("references", "documents", "mentions")),
)


# ---------------------------------------------------------------------------
# Alias 정규화 — 동의어/역방향 어휘를 canonical 로 수렴
# ---------------------------------------------------------------------------
# 결정론 추출기(body_extractor, link_graph_builder, ast_code_extractor)는
# 자체 어휘 상수를 그대로 emit 하고, GraphStore 저장 시점(+ llm_body_extractor
# 파싱 시점)에 아래 매핑으로 한 번 정규화된다. alias 의 canonical 대상은
# 반드시 ENTITY_TYPES / RELATION_TYPES 에 존재해야 한다 (테스트가 강제).

ENTITY_TYPE_ALIASES: dict[str, str] = {
    # Go struct 를 별도 타입으로 두면 언어별로 같은 개념이 갈라져
    # (name, entity_type) 병합 키가 깨진다.
    "struct": "class",
    # 정책은 개념의 부분집합 — LLM 이 양쪽으로 흔들리며 노드를 분열시킴.
    "policy": "concept",
}


@dataclass(frozen=True)
class RelationAlias:
    """relation_type alias 한 건.

    Attributes:
        canonical: 정규화 후 relation_type 이름.
        swap_direction: True 면 source/target 을 교환해 canonical 방향으로
            저장한다 (예: ``documented_in(A, B)`` ≡ ``documents(B, A)``).
    """

    canonical: str
    swap_direction: bool = False


RELATION_TYPE_ALIASES: dict[str, RelationAlias] = {
    "has_part": RelationAlias("contains"),
    "has_attribute": RelationAlias("contains"),
    "uses": RelationAlias("depends_on"),
    # 타깃 entity_type(person/ticket)이 이미 대상 의미를 담고 있어
    # 관계 이름의 접미사는 중복 인코딩이었다.
    "mentions_user": RelationAlias("mentions"),
    "mentions_ticket": RelationAlias("mentions"),
    # documents 와 서로 역방향인 같은 관계 — 방향을 canonical 로 통일.
    "documented_in": RelationAlias("documents", swap_direction=True),
}


def canonical_entity_type(entity_type: str) -> str:
    """entity_type 을 canonical 이름으로 정규화한다 (alias 아니면 그대로)."""
    return ENTITY_TYPE_ALIASES.get(entity_type, entity_type)


def canonical_relation_type(relation_type: str) -> str:
    """relation_type 이름만 정규화한다 (방향 정보 불필요한 소비자용)."""
    alias = RELATION_TYPE_ALIASES.get(relation_type)
    return alias.canonical if alias else relation_type


def canonical_relation(
    relation_type: str, source: str, target: str,
) -> tuple[str, str, str]:
    """relation 을 canonical ``(type, source, target)`` 으로 정규화한다.

    역방향 alias(``documented_in``)는 source/target 을 교환한다.
    """
    alias = RELATION_TYPE_ALIASES.get(relation_type)
    if alias is None:
        return relation_type, source, target
    if alias.swap_direction:
        return alias.canonical, target, source
    return alias.canonical, source, target


def all_known_entity_type_names() -> set[str]:
    """canonical + alias 전체 이름 — 추출기 어휘 포함 검증용."""
    return all_entity_type_names() | set(ENTITY_TYPE_ALIASES)


def all_known_relation_type_names() -> set[str]:
    """canonical + alias 전체 이름 — 추출기 어휘 포함 검증용."""
    return all_relation_type_names() | set(RELATION_TYPE_ALIASES)


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


# ---------------------------------------------------------------------------
# Subset filters by extractor source
# ---------------------------------------------------------------------------
# 인덱싱 측 추출기 (llm_body_extractor, link_graph_builder, ast_code 등) 마다
# 어휘 범위가 다르다. 인덱싱-검색 LLM 정렬을 위해, 각 추출기는 자신의 subset
# 만 LLM 프롬프트에 노출해야 한다 — 그렇지 않으면 인덱싱 LLM 이 추출 안 한
# 타입을 답할 위험. 검색 LLM 은 모든 subset 의 union 을 본다 (그래프에는 모든
# 추출기 결과가 합쳐져 있으므로).


def _has_source(entry: VocabEntry, keyword: str) -> bool:
    return keyword in entry.source


def llm_body_entity_types_vocab() -> tuple[VocabEntry, ...]:
    """LLM 본문 추출기 (``llm_body_extractor``) 가 사용하는 entity_type subset.

    ``source`` 필드에 ``"llm_body"`` 가 포함된 entry 만 반환.
    """
    return tuple(e for e in ENTITY_TYPES if _has_source(e, "llm_body"))


def llm_body_relation_types_vocab() -> tuple[VocabEntry, ...]:
    """LLM 본문 추출기가 사용하는 relation_type subset."""
    return tuple(r for r in RELATION_TYPES if _has_source(r, "llm_body"))


def llm_body_entity_type_names() -> tuple[str, ...]:
    """LLM 본문 추출기 entity_type 이름 목록 (config 호환용)."""
    return tuple(e.name for e in llm_body_entity_types_vocab())


def llm_body_relation_type_names() -> tuple[str, ...]:
    """LLM 본문 추출기 relation_type 이름 목록 (config 호환용)."""
    return tuple(r.name for r in llm_body_relation_types_vocab())


def format_vocab_entries_for_prompt(entries: tuple[VocabEntry, ...]) -> str:
    """임의의 ``VocabEntry`` 튜플을 LLM 프롬프트용 텍스트로 변환.

    검색·인덱싱 LLM 모두 동일 포맷 (``- **name**: description``) 으로 어휘를
    보도록 한다 — mental model 정합.
    """
    return "\n".join(f"- **{e.name}**: {e.description}" for e in entries)
