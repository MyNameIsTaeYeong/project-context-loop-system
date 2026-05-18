"""ExtractionUnit 본문 → 결정론적 엔티티/관계 추출기.

LLM 호출 없이 본문에서 4가지 시그널을 추출하여 ``GraphData`` 로 변환한다.

1. HTTP API 엔드포인트 (``POST /v1/payments`` 등)      → ``api``       [기본 ON]
2. Jira 이슈 키 (``PROJ-123``)                         → ``ticket``    [기본 ON]
3. 굵게 강조된 용어 (``**X**`` / ``__X__``)            → ``concept``   [기본 OFF]
4. 마크다운 테이블의 헤더 셀                           → ``concept``   [기본 OFF]

기본 활성/비활성 정책
---------------------
구조적 신호(API 엔드포인트, Jira 키)는 형식이 명확하고 작성자 의도가
강하므로 기본 ON. 반면 강조 용어와 표 헤더는 작성 컨벤션 의존도가
높고 노이즈("Method", "필수", "예시" 같은 추상 헤더 / 단순 강조)가
많아 기본 OFF. 의미 관계 추출은 LLM 추출기에 위임한다.

링크 그래프(``link_graph_builder``)와 마찬가지로 자기 문서를
``Entity(name=doc_title, entity_type="document")`` 로 등록하고, 본문에서
추출한 모든 엔티티를 ``mentions`` / ``documents`` / ``has_attribute`` /
``mentions_ticket`` 관계로 연결한다. ``GraphStore`` 의 정규 노드 병합 덕분에
링크 그래프와 본문 그래프가 같은 ``document`` 노드로 자연 수렴한다.

Relation.label 은 첫 등장 unit 의 ``section_path`` 를 기록한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from context_loop.processor.chunker import _TABLE_SEPARATOR_RE
from context_loop.processor.extraction_unit import ExtractionUnit
from context_loop.processor.graph_extractor import Entity, GraphData, Relation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 정규식
# ---------------------------------------------------------------------------

_BOLD_RE = re.compile(r"\*\*([^\n*]+?)\*\*|__([^\n_]+?)__")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_API_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+"
    r"(/[A-Za-z0-9_\-/{}:.~]+)"
)
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")

_BAD_BOLD_CHARS = set("\t")  # tab/제어문자 제외
_API_PATH_TRIM = "`'\").,;:"

# 헤더로 받기엔 의미 없는 짧은 셀 (예: "v" / "1" 등)은 추출 가치가 낮음.
_MIN_HEADER_LEN = 2
# 표 헤더가 너무 많으면(예: 12열 이상) 매트릭스 류여서 의미 추출이 어렵다.
_MAX_TABLE_COLUMNS = 12


@dataclass(frozen=True)
class BodyExtractionConfig:
    """본문 추출기 옵션.

    기본값 정책: 구조적 신호(API/Jira)는 ON, 휴리스틱 신호(강조/표 헤더)는
    OFF. 의미 관계 추출은 LLM 추출기에 위임한다 (모듈 docstring 참조).

    Attributes:
        extract_api_endpoints: ``GET /path`` 형태 API 엔드포인트 추출
            (기본 ON — 형식이 명확하고 작성자 의도가 강함).
        extract_jira_keys: ``PROJ-123`` 형태 Jira 키를 ``ticket`` 으로 추출
            (기본 ON — 정규 표현식으로 정확).
        extract_bold_terms: ``**X**`` / ``__X__`` 굵게 강조 용어를
            ``concept`` 로 추출 (기본 OFF — 작성 컨벤션 의존, 노이즈 많음).
        extract_table_headers: 마크다운 테이블 헤더 셀을 ``concept`` 로
            추출 (기본 OFF — "Method", "필수" 같은 추상 헤더가 노이즈).
        bold_min_length: 강조 용어 최소 글자 수 (이 미만 스킵).
        bold_max_length: 강조 용어 최대 글자 수 (초과 시 문장으로 보고 스킵).
        max_table_columns: 표 헤더 셀 수 상한 (초과 시 표 전체 스킵).
        skip_inside_code_blocks: 펜스 코드블록 내부 텍스트는 추출 대상에서
            제외할지 여부. 단, ``extract_api_endpoints`` 만은 코드블록 내부도
            스캔한다 (코드 예제에 API 가 자주 있음).
    """

    extract_api_endpoints: bool = True
    extract_jira_keys: bool = True
    extract_bold_terms: bool = False
    extract_table_headers: bool = False
    bold_min_length: int = 2
    bold_max_length: int = 60
    max_table_columns: int = _MAX_TABLE_COLUMNS
    skip_inside_code_blocks: bool = True


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def extract_body_graph(
    units: list[ExtractionUnit],
    *,
    doc_title: str,
    config: BodyExtractionConfig | None = None,
) -> GraphData:
    """ExtractionUnit 목록에서 결정론적으로 본문 엔티티/관계를 추출한다.

    Args:
        units: 같은 문서의 ExtractionUnit 목록.
        doc_title: 문서 제목 (self-entity 이름 + 모든 관계의 source 로 사용).
        config: 추출 옵션. None 이면 기본값.

    Returns:
        ``GraphData``. 추출된 엔티티가 하나도 없으면 빈 그래프를 반환한다.
        ``link_graph_builder`` 와 같은 정책 — 의미 있는 시그널이 없으면
        self-entity 도 emit 하지 않는다.
    """
    cfg = config or BodyExtractionConfig()
    if not doc_title or not units:
        return GraphData()

    # 엔티티: (name_lower, entity_type) → Entity
    entities: dict[tuple[str, str], Entity] = {}
    # 관계: (source_lower, target_lower, relation_type) → Relation (link_graph_builder 패턴)
    relations: dict[tuple[str, str, str], Relation] = {}

    self_key = (doc_title.lower(), "document")
    self_entity = Entity(name=doc_title, entity_type="document")

    for unit in units:
        section_path_label = " > ".join(unit.section_path)
        prose = _strip_code_for_prose(unit.body) if cfg.skip_inside_code_blocks else unit.body

        if cfg.extract_bold_terms:
            for term in _find_bold_terms(prose, cfg):
                _add(
                    entities, relations, self_entity,
                    target_name=term, target_type="concept",
                    relation_type="mentions",
                    label=section_path_label,
                )

        if cfg.extract_api_endpoints:
            for endpoint in _find_api_endpoints(unit.body):
                _add(
                    entities, relations, self_entity,
                    target_name=endpoint, target_type="api",
                    relation_type="documents",
                    label=section_path_label,
                )

        if cfg.extract_table_headers:
            for header in _find_table_headers(unit.body, cfg):
                _add(
                    entities, relations, self_entity,
                    target_name=header, target_type="concept",
                    relation_type="has_attribute",
                    label=section_path_label,
                )

        if cfg.extract_jira_keys:
            for key in _find_jira_keys(prose):
                _add(
                    entities, relations, self_entity,
                    target_name=key, target_type="ticket",
                    relation_type="mentions_ticket",
                    label=section_path_label,
                )

    if not relations:
        # 자기 자신 외에 추출된 관계가 없으면 그래프를 비운다.
        return GraphData()

    # self-entity 는 첫 번째로 등장하도록 prepend
    entity_list: list[Entity] = [self_entity]
    for entity_key, ent in entities.items():
        if entity_key != self_key:
            entity_list.append(ent)

    return GraphData(entities=entity_list, relations=list(relations.values()))


# ---------------------------------------------------------------------------
# 패턴 추출
# ---------------------------------------------------------------------------


def _find_bold_terms(text: str, cfg: BodyExtractionConfig) -> list[str]:
    """``**X**`` / ``__X__`` 강조 용어를 등장 순서대로 반환한다 (중복 제거)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _BOLD_RE.finditer(text):
        raw = m.group(1) or m.group(2) or ""
        term = _normalize_term(raw)
        if not _is_valid_bold_term(term, cfg):
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _is_valid_bold_term(term: str, cfg: BodyExtractionConfig) -> bool:
    if len(term) < cfg.bold_min_length or len(term) > cfg.bold_max_length:
        return False
    if any(ch in _BAD_BOLD_CHARS for ch in term):
        return False
    # 숫자/기호만 있는 경우 제외 (의미 부족)
    if not any(ch.isalpha() or _is_cjk(ch) for ch in term):
        return False
    return True


def _is_cjk(ch: str) -> bool:
    """한자/한글/일본어 등 CJK 문자 여부 (간이 판정)."""
    cp = ord(ch)
    return (
        0x3000 <= cp <= 0x9FFF       # CJK + 한글 자모/완성형 일부
        or 0xAC00 <= cp <= 0xD7AF    # 한글 음절
    )


def _find_api_endpoints(text: str) -> list[str]:
    """``METHOD /path`` 패턴을 등장 순서대로 반환한다 (중복 제거)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _API_RE.finditer(text):
        method = m.group(1).upper()
        path = m.group(2).rstrip(_API_PATH_TRIM).rstrip("/")
        if not path or path == "/":
            continue
        endpoint = f"{method} {path}"
        if endpoint in seen:
            continue
        seen.add(endpoint)
        out.append(endpoint)
    return out


def _find_table_headers(text: str, cfg: BodyExtractionConfig) -> list[str]:
    """마크다운 테이블의 헤더 셀들을 반환한다 (중복 제거).

    구분자(``|---|``) 행 직전 행을 헤더로 본다. 같은 텍스트가 여러 표에서
    나오면 한 번만 emit 한다.
    """
    seen: set[str] = set()
    out: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines[:-1]):
        if not _TABLE_SEPARATOR_RE.match(lines[i + 1]):
            continue
        cells = _split_table_row(line)
        if len(cells) < 2 or len(cells) > cfg.max_table_columns:
            continue
        for cell in cells:
            term = _normalize_term(cell)
            if len(term) < _MIN_HEADER_LEN:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
    return out


def _split_table_row(line: str) -> list[str]:
    """``| a | b | c |`` → ``["a", "b", "c"]``. 빈 셀은 제외."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|") if c.strip()]


def _find_jira_keys(text: str) -> list[str]:
    """Jira 키(``PROJ-123``)를 등장 순서대로 반환한다 (중복 제거)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _JIRA_RE.finditer(text):
        key = m.group(1)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _strip_code_for_prose(text: str) -> str:
    """펜스 코드블록과 인라인 코드를 제거한 텍스트를 반환한다.

    굵게 강조 / Jira 키 추출은 코드 영역을 보지 말아야 한다 (예제 코드의 변수/주석에
    있는 패턴이 잘못 잡히는 것을 방지).
    """
    no_fence = _CODE_FENCE_RE.sub("\n", text)
    return _INLINE_CODE_RE.sub(" ", no_fence)


def _normalize_term(term: str) -> str:
    """양 끝 공백 + 흔한 트림 문자 제거."""
    return term.strip().strip("·•:,.;\"'`")


def _add(
    entities: dict[tuple[str, str], Entity],
    relations: dict[tuple[str, str, str], Relation],
    self_entity: Entity,
    *,
    target_name: str,
    target_type: str,
    relation_type: str,
    label: str,
) -> None:
    """엔티티/관계를 dedup 키 기준으로 등록한다."""
    if not target_name:
        return
    self_key = (self_entity.name.lower(), self_entity.entity_type)
    target_key = (target_name.lower(), target_type)

    # self-entity 는 처음 한 번만 등록
    if self_key not in entities:
        entities[self_key] = self_entity

    # target entity 등록 (대소문자 무시 dedup; 첫 등장 표기를 보존)
    if target_key not in entities:
        entities[target_key] = Entity(
            name=target_name, entity_type=target_type, description="",
        )

    rel_key = (
        self_entity.name.lower(),
        target_name.lower(),
        relation_type,
    )
    if rel_key not in relations:
        relations[rel_key] = Relation(
            source=self_entity.name,
            target=entities[target_key].name,
            relation_type=relation_type,
            label=label,
        )
