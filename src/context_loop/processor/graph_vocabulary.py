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

import re
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
    VocabEntry(
        "module",
        "시스템 내부 컴포넌트 또는 코드 파일/패키지 (예: Token Validator, "
        "user_service.py)",
        "llm_body + ast_code",
    ),
    VocabEntry("policy", "정책/규칙", "llm_body"),
    VocabEntry("team", "팀/조직", "llm_body"),
    # AST 코드 추출
    VocabEntry("function", "함수 심볼 (git_code 추출)", "ast_code"),
    VocabEntry("class", "클래스 심볼 (git_code 추출)", "ast_code"),
    VocabEntry("method", "클래스/구조체에 속한 메서드 심볼 (git_code 추출)",
               "ast_code"),
    VocabEntry("struct", "Go 구조체 (git_code 추출)", "ast_code"),
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
    VocabEntry("imports", "모듈이 다른 모듈을 import", "ast_code"),
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


# ---------------------------------------------------------------------------
# Alias 정규화 (F-CG2-08) + 이름 stem 정규화 (F-CG2-06)
# ---------------------------------------------------------------------------
# LLM 출력의 형태론적 변형(``depending_on`` 등)이나 표기 변형
# (``AuthService``/``Auth Service``/``auth-service``)을 canonical 어휘 또는
# 단일 stem 키로 통합하여, vocab strict 검증과 dedup 키가 의도된 노드/엣지를
# 잃지 않도록 한다. 보수적 화이트리스트만 등록 — 의미 위험(방향 반전, 도메인
# 모호성)이 있는 매핑은 의도적으로 제외한다.

ENTITY_TYPE_ALIASES: dict[str, str] = {
    # 복수형/단수형/직접 동의어 — 의미 위험 낮은 매핑만 등록.
    # ``service``, ``user``, ``group`` 등은 도메인 충돌 위험이 있어 제외.
    "policies": "policy",
    "rules": "policy",
    "component": "module",
    "components": "module",
}

RELATION_TYPE_ALIASES: dict[str, str] = {
    # 동명사/현재형/형용사 변형 (F-CG2-08 보고서 인용)
    "depending_on": "depends_on",
    "depend_on": "depends_on",
    "dependent_on": "depends_on",
    # 방향 동일한 동의어/시제 변형
    "owns": "owned_by",
    "owner": "owned_by",
    "implement": "implements",
    "implementing": "implements",
    "documents_in": "documented_in",
    "described_in": "documented_in",
    # 방향 반전(``has_part`` ↔ ``part_of``, ``supersedes`` ↔ ``replaces``)은
    # 단순 type 매핑으로 처리하면 source/target 의미가 뒤집히므로 제외.
}


def normalize_entity_type(raw: str) -> str:
    """LLM 출력 entity_type 을 canonical 어휘로 정규화.

    ``raw.strip().lower()`` 후 :data:`ENTITY_TYPE_ALIASES` 매핑을 적용한다.
    매핑이 없으면 ``raw.strip()`` 를 그대로 반환한다 — 호출자가 이후 vocab
    화이트리스트와 비교한다.
    """
    key = raw.strip().lower()
    return ENTITY_TYPE_ALIASES.get(key, raw.strip())


def normalize_relation_type(raw: str) -> str:
    """LLM 출력 relation_type 을 canonical 어휘로 정규화. 동일 정책."""
    key = raw.strip().lower()
    return RELATION_TYPE_ALIASES.get(key, raw.strip())


# 표기 변형(공백 / 하이픈 / 언더스코어) 만 정규화하는 패턴.
# 형태론적 변형(복수형/동사형) 은 의도적으로 제외 — 의미 경계가 모호한 통합을
# 피하기 위한 보수적 정책.
_NAME_STEM_PUNCT_RE = re.compile(r"[\s\-_]+")


def normalize_name_stem(name: str) -> str:
    """표기 변형(공백/하이픈/언더스코어/대소문자)을 통합한 stem 키 생성.

    예) ``AuthService`` / ``Auth Service`` / ``auth-service`` / ``auth_service``
    → ``"authservice"``.

    *형태론적 변형* (예: ``User Service`` vs ``Users``) 은 *통합하지 않는다*.
    의미 경계가 모호한 통합을 피하기 위한 보수적 정책.
    """
    return _NAME_STEM_PUNCT_RE.sub("", name).lower()
