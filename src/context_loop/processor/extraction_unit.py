"""본문 기반 엔티티/관계 추출의 입력 단위 (Extraction Unit) 빌더.

벡터 검색 청크(``chunker``)와는 별도로, 그래프 추출에 적합한 입자도(약
1500 토큰)의 단위를 생성한다. 핵심 아이디어:

- ``ExtractedDocument.sections`` 의 헤딩 트리를 복원한다.
- 후위 순회로 각 노드의 ``merged_tokens`` (자기 + 모든 자손 토큰 합)을 계산한다.
- 상위에서 내려가며 ``merged_tokens <= target_tokens`` 인 첫 노드를 한 unit으로
  응축(condense)한다. 이렇게 하면 미니 H4/H5는 부모 H3 아래로 자연 흡수된다.
- ``own_tokens > max_tokens`` 인 거대 단일 섹션은 문단 경계에서 분할하며
  ``overlap_tokens`` 만큼 직전 part 꼬리를 다음 part 머리로 복제한다.
- 부모 자기 본문이 짧고(``< min_tokens``) 자식이 있으면, 부모 본문을 첫 자식
  unit의 머리에 prepend 한다 (section_ids 에도 부모를 포함).
- 각 unit content 앞에는 문서 제목/섹션 경로/머리말 등 상위 문맥(breadcrumb)을
  주입하여, 추출 LLM이 unit만 보고도 "어디에 관한 이야기인지" 파악할 수 있게 한다.

추출 결과의 출처 추적을 위해 각 unit은 안정적 ``section_ids``
(``f"{document_id}:{section_index}"``) 를 보유한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from context_loop.ingestion.confluence_extractor import ExtractedDocument, Section
from context_loop.processor.chunker import (
    _TABLE_SEPARATOR_RE,
    _Block,
    _get_tokenizer,
    _split_markdown_blocks,
    count_tokens,
)

_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


@dataclass(frozen=True)
class ExtractionUnitConfig:
    """Extraction Unit 빌드 파라미터.

    Attributes:
        target_tokens: unit 목표 크기. 응축/분할 결정의 기준값.
        max_tokens: unit 상한. 단일 섹션 본문이 이를 초과하면 강제 분할.
        min_tokens: 부모 자기 본문이 이 미만이면 첫 자식 unit에 prepend.
        overlap_tokens: 거대 섹션 분할 시 인접 part 간 토큰 overlap.
        breadcrumb_doc_title: content 상단에 ``# 문서: <title>`` 포함.
        breadcrumb_path: content 상단에 ``## 위치: A > B > C`` 포함.
        include_lead_paragraph: 첫 헤딩 이전 머리말을 모든 unit에 prefix.
        lead_paragraph_max_tokens: 머리말 최대 토큰 (초과 시 토큰 단위 절단).
        encoding_model: tiktoken 인코딩 이름.
    """

    target_tokens: int = 1500
    max_tokens: int = 2400
    min_tokens: int = 400
    overlap_tokens: int = 200
    breadcrumb_doc_title: bool = True
    breadcrumb_path: bool = True
    include_lead_paragraph: bool = True
    lead_paragraph_max_tokens: int = 200
    encoding_model: str = "cl100k_base"


@dataclass(frozen=True)
class ExtractionUnit:
    """본문 기반 엔티티/관계 추출의 입력 단위.

    Attributes:
        unit_id: 문서 내 고유 식별자. ``f"{document_id}:{ordinal:04d}"``.
        document_id: 소속 문서 ID.
        ordinal: 문서 내 0-based 순서.
        section_ids: 이 unit이 커버하는 섹션 ID 튜플
            (``f"{document_id}:{section_index}"`` 포맷).
        primary_section_id: 대표 섹션 ID (병합 루트 또는 분할 본 섹션).
        section_path: 대표 섹션의 헤딩 경로.
        breadcrumb: content 상단에 주입된 문맥 텍스트.
        content: ``breadcrumb + body`` 결합 결과. LLM 입력으로 그대로 사용.
        body: breadcrumb 제외한 순수 본문 (디버그/검증용).
        token_count: ``content`` 기준 토큰 수.
        has_table: body에 마크다운 테이블 포함 여부.
        has_code_block: body에 펜스 코드블록 포함 여부.
        split_part: 거대 섹션 분할 시 0-based part 번호.
        split_total: 거대 섹션 분할 시 전체 part 수.
    """

    unit_id: str
    document_id: int
    ordinal: int
    section_ids: tuple[str, ...]
    primary_section_id: str
    section_path: tuple[str, ...]
    breadcrumb: str
    content: str
    body: str
    token_count: int
    has_table: bool
    has_code_block: bool
    split_part: int = 0
    split_total: int = 1


# ---------------------------------------------------------------------------
# 내부 트리 표현
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    """섹션 트리의 한 노드. 빌드 과정 동안만 사용되는 가변 구조.

    ``own_body`` 는 self-render 결과(헤딩 + 본문)를 캐시한다 — 트리 빌드 시
    한 번 만들어 두고 collect/render 단계에서 재사용해 중복 문자열 결합을
    피한다.
    """

    section_index: int
    section: Section
    own_body: str
    own_tokens: int
    merged_tokens: int = 0
    children: list[_Node] = field(default_factory=list)
    parent: _Node | None = None


@dataclass
class _PreUnit:
    """breadcrumb 주입 전 중간 표현."""

    section_ids: tuple[str, ...]
    primary_section_id: str
    section_path: tuple[str, ...]
    body: str
    has_table: bool
    has_code_block: bool
    split_part: int = 0
    split_total: int = 1


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def build_extraction_units(
    extracted: ExtractedDocument,
    *,
    document_id: int,
    doc_title: str,
    config: ExtractionUnitConfig | None = None,
) -> list[ExtractionUnit]:
    """``ExtractedDocument`` 를 추출용 unit 목록으로 변환한다.

    Args:
        extracted: Confluence 추출 결과.
        document_id: 소속 문서 ID. unit_id/section_id 생성에 사용.
        doc_title: 문서 제목. breadcrumb 와 sections-less 폴백에 사용.
        config: 빌드 파라미터. None 이면 기본값.

    Returns:
        ``ExtractionUnit`` 목록. 입력이 비어 있으면 빈 리스트.
    """
    cfg = config or ExtractionUnitConfig()
    encode, decode = _make_codec(cfg.encoding_model)

    lead_paragraph = _extract_lead_paragraph(extracted.plain_text) \
        if cfg.include_lead_paragraph else ""

    # sections 가 없으면 plain_text 만으로 unit 을 만든다.
    if not extracted.sections:
        return _build_from_plain_text(
            extracted.plain_text,
            document_id=document_id,
            doc_title=doc_title,
            lead_paragraph=lead_paragraph,
            cfg=cfg,
            encode=encode,
            decode=decode,
        )

    roots = _build_tree(extracted.sections, document_id=document_id, cfg=cfg)
    for root in roots:
        _compute_merged_tokens(root)

    pre_units: list[_PreUnit] = []
    for root in roots:
        pre_units.extend(
            _collect_units(root, document_id=document_id, cfg=cfg, encode=encode, decode=decode)
        )

    return _finalize(
        pre_units,
        document_id=document_id,
        doc_title=doc_title,
        lead_paragraph=lead_paragraph,
        cfg=cfg,
        encode=encode,
        decode=decode,
    )


# ---------------------------------------------------------------------------
# Step A: 트리 구성 + own_tokens 계산
# ---------------------------------------------------------------------------


def _build_tree(
    sections: list[Section],
    *,
    document_id: int,
    cfg: ExtractionUnitConfig,
) -> list[_Node]:
    """평면 sections 리스트에서 부모-자식 트리(루트 목록)를 복원한다.

    각 ``Section.level`` 은 H1=1, H2=2, ... 이며 등장 순서가 곧 깊이 우선
    순회 순서이다. 스택을 사용해 같은 레벨 이상의 형제/부모를 pop 하며
    부모를 결정한다.

    self-render 결과는 ``Node.own_body`` 에 캐시하여 collect/render 단계의
    중복 문자열 결합을 피한다 (이전 구현은 트리 빌드 시 던졌다가 collect
    단계에서 동일 문자열을 다시 만들었다).
    """
    roots: list[_Node] = []
    stack: list[_Node] = []
    for idx, section in enumerate(sections):
        own_body = _self_render(section)
        own_tokens = count_tokens(own_body, cfg.encoding_model) if own_body else 0
        node = _Node(
            section_index=idx,
            section=section,
            own_body=own_body,
            own_tokens=own_tokens,
        )
        while stack and stack[-1].section.level >= section.level:
            stack.pop()
        if stack:
            parent = stack[-1]
            parent.children.append(node)
            node.parent = parent
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _self_render(section: Section) -> str:
    """섹션 자기 자신(자손 제외)의 마크다운 렌더링."""
    parts: list[str] = []
    if section.title:
        parts.append("#" * max(section.level, 1) + " " + section.title)
    body = section.md_content.strip()
    if body:
        parts.append(body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Step B: bottom-up merged_tokens
# ---------------------------------------------------------------------------


def _compute_merged_tokens(node: _Node) -> int:
    total = node.own_tokens
    for child in node.children:
        total += _compute_merged_tokens(child)
    node.merged_tokens = total
    return total


# ---------------------------------------------------------------------------
# Step B/C: top-down 응축/분할 + 부모 own 흡수
# ---------------------------------------------------------------------------


def _collect_units(
    node: _Node,
    *,
    document_id: int,
    cfg: ExtractionUnitConfig,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> list[_PreUnit]:
    """노드를 처리하여 _PreUnit 목록을 반환한다.

    1) merged_tokens <= target → 서브트리 전체를 한 unit 으로 응축.
    2) own_tokens > max → 자기 본문을 분할(여러 unit), 자식은 별도 처리.
    3) own_tokens > 0 → 자기 본문을 단독 unit 으로 emit (단,
       own_tokens < min_tokens AND 자식 존재 시 첫 자식 unit 머리로 흡수).
    4) 자식들 재귀 처리.

    성능: ``own_body`` 캐시 + 응축 분기에서 단일 walk 로 body/section_ids
    동시 생성 (이전 구현은 ``_render_subtree`` + ``_dfs`` 가 같은 서브트리를
    각각 한 번씩 순회, 매 descendant 마다 ``_self_render`` 재계산).
    """
    if node.merged_tokens <= cfg.target_tokens:
        body, section_ids = _render_and_collect_ids(node, document_id)
        return [_PreUnit(
            section_ids=section_ids,
            primary_section_id=_section_id(document_id, node.section_index),
            section_path=tuple(node.section.path),
            body=body,
            has_table=_detect_table(body),
            has_code_block=_detect_code_block(body),
        )]

    units: list[_PreUnit] = []
    absorb_pending: tuple[_Node, str] | None = None

    if node.own_tokens > cfg.max_tokens:
        # 자기 본문을 분할 → 부모 own 흡수는 적용 안 함
        parts = _split_oversized(node.own_body, cfg=cfg, encode=encode, decode=decode)
        path = tuple(node.section.path)
        sid = _section_id(document_id, node.section_index)
        for i, part_body in enumerate(parts):
            units.append(_PreUnit(
                section_ids=(sid,),
                primary_section_id=sid,
                section_path=path,
                body=part_body,
                has_table=_detect_table(part_body),
                has_code_block=_detect_code_block(part_body),
                split_part=i,
                split_total=len(parts),
            ))
    elif node.own_tokens > 0:
        own_body = node.own_body
        if node.own_tokens < cfg.min_tokens and node.children:
            # 첫 자식 unit 머리로 흡수
            absorb_pending = (node, own_body)
        else:
            sid = _section_id(document_id, node.section_index)
            units.append(_PreUnit(
                section_ids=(sid,),
                primary_section_id=sid,
                section_path=tuple(node.section.path),
                body=own_body,
                has_table=_detect_table(own_body),
                has_code_block=_detect_code_block(own_body),
            ))

    for child in node.children:
        child_units = _collect_units(
            child, document_id=document_id, cfg=cfg, encode=encode, decode=decode,
        )
        if absorb_pending and child_units:
            parent_node, parent_body = absorb_pending
            first = child_units[0]
            new_body = parent_body + "\n\n" + first.body
            parent_sid = _section_id(document_id, parent_node.section_index)
            new_section_ids = (parent_sid,) + first.section_ids
            child_units[0] = _PreUnit(
                section_ids=new_section_ids,
                primary_section_id=first.primary_section_id,
                section_path=first.section_path,
                body=new_body,
                has_table=first.has_table or _detect_table(parent_body),
                has_code_block=first.has_code_block or _detect_code_block(parent_body),
                split_part=first.split_part,
                split_total=first.split_total,
            )
            absorb_pending = None
        units.extend(child_units)

    if absorb_pending:
        # 자식이 unit 을 만들지 못한 엣지 케이스 → 부모 단독 emit
        parent_node, parent_body = absorb_pending
        sid = _section_id(document_id, parent_node.section_index)
        units.append(_PreUnit(
            section_ids=(sid,),
            primary_section_id=sid,
            section_path=tuple(parent_node.section.path),
            body=parent_body,
            has_table=_detect_table(parent_body),
            has_code_block=_detect_code_block(parent_body),
        ))

    return units


def _render_and_collect_ids(
    node: _Node, document_id: int,
) -> tuple[str, tuple[str, ...]]:
    """서브트리 한 번 순회로 (rendered body, section_ids 튜플) 동시 생성.

    각 노드의 ``own_body`` 캐시를 그대로 사용 — 재렌더 비용 없음. 이전 구현은
    ``_render_subtree`` 와 ``_dfs`` 가 별도로 같은 서브트리를 두 번 순회했다.
    """
    parts: list[str] = []
    ids: list[str] = []

    def walk(n: _Node) -> None:
        ids.append(_section_id(document_id, n.section_index))
        if n.own_body:
            parts.append(n.own_body)
        for child in n.children:
            walk(child)

    walk(node)
    return "\n\n".join(parts), tuple(ids)


# ---------------------------------------------------------------------------
# Step C: 거대 단일 섹션 분할
# ---------------------------------------------------------------------------


def _split_oversized(
    text: str,
    *,
    cfg: ExtractionUnitConfig,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> list[str]:
    """단일 섹션 본문을 문단(blank line) 경계에서 그리디 분할한다.

    - 펜스 코드블록 / 마크다운 테이블은 atomic 으로 보호되어 절대 분할되지 않는다
      (max_tokens 를 단독 초과해도 그대로 한 part 로 둠).
    - 일반 문단은 누적 토큰이 ``target_tokens`` 를 초과하기 직전에 컷.
    - 컷 직후 part 는 직전 part 꼬리에서 ``overlap_tokens`` 만큼 복제한다.
    """
    blocks = _split_markdown_blocks(text)
    if not blocks:
        return []

    # 그리디 패킹
    parts: list[list[_Block]] = [[]]
    current_tokens = 0
    for block in blocks:
        b_tokens = count_tokens(block.content, cfg.encoding_model)

        # 현재 part 에 누적이 있고, 추가 시 target 초과면 컷
        if parts[-1] and current_tokens + b_tokens > cfg.target_tokens:
            parts.append([])
            current_tokens = 0

        parts[-1].append(block)
        current_tokens += b_tokens

    parts = [p for p in parts if p]

    # part 별 텍스트 렌더 + overlap 머리 복제
    rendered: list[str] = []
    for i, part in enumerate(parts):
        body = "\n\n".join(b.content for b in part)
        if i > 0 and cfg.overlap_tokens > 0:
            prev_text = rendered[-1]
            tail = _take_tail_tokens(prev_text, cfg.overlap_tokens, encode, decode)
            if tail:
                body = tail + "\n\n" + body
        rendered.append(body)
    return rendered


def _take_tail_tokens(
    text: str,
    n: int,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> str:
    if n <= 0 or not text:
        return ""
    tokens = encode(text)
    if len(tokens) <= n:
        return text
    return decode(tokens[-n:])


# ---------------------------------------------------------------------------
# Step D: breadcrumb 주입 + 최종 ExtractionUnit 생성
# ---------------------------------------------------------------------------


def _finalize(
    pre_units: list[_PreUnit],
    *,
    document_id: int,
    doc_title: str,
    lead_paragraph: str,
    cfg: ExtractionUnitConfig,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> list[ExtractionUnit]:
    truncated_lead = _truncate_to_tokens(
        lead_paragraph, cfg.lead_paragraph_max_tokens, encode, decode,
    ) if (cfg.include_lead_paragraph and lead_paragraph) else ""

    out: list[ExtractionUnit] = []
    for ordinal, pu in enumerate(pre_units):
        breadcrumb = _build_breadcrumb(
            section_path=pu.section_path,
            split_part=pu.split_part,
            split_total=pu.split_total,
            doc_title=doc_title,
            lead_paragraph=truncated_lead,
            cfg=cfg,
        )
        if breadcrumb:
            content = breadcrumb + "\n\n---\n\n" + pu.body
        else:
            content = pu.body
        unit_id = f"{document_id}:{ordinal:04d}"
        out.append(ExtractionUnit(
            unit_id=unit_id,
            document_id=document_id,
            ordinal=ordinal,
            section_ids=pu.section_ids,
            primary_section_id=pu.primary_section_id,
            section_path=pu.section_path,
            breadcrumb=breadcrumb,
            content=content,
            body=pu.body,
            token_count=count_tokens(content, cfg.encoding_model),
            has_table=pu.has_table,
            has_code_block=pu.has_code_block,
            split_part=pu.split_part,
            split_total=pu.split_total,
        ))
    return out


def _build_breadcrumb(
    *,
    section_path: tuple[str, ...],
    split_part: int,
    split_total: int,
    doc_title: str,
    lead_paragraph: str,
    cfg: ExtractionUnitConfig,
) -> str:
    lines: list[str] = []
    if cfg.breadcrumb_doc_title and doc_title:
        lines.append(f"# 문서: {doc_title}")
    if cfg.breadcrumb_path and section_path:
        lines.append(f"## 위치: {' > '.join(section_path)}")
    if split_total > 1:
        lines.append(f"## 부분: {split_part + 1}/{split_total}")
    if lead_paragraph:
        lines.append(f"## 문서 요약\n{lead_paragraph}")
    return "\n\n".join(lines)


def _truncate_to_tokens(
    text: str,
    max_tokens: int,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> str:
    if max_tokens <= 0 or not text:
        return ""
    tokens = encode(text)
    if len(tokens) <= max_tokens:
        return text
    return decode(tokens[:max_tokens]).rstrip() + "..."


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _section_id(document_id: int, section_index: int) -> str:
    return f"{document_id}:{section_index}"


def _detect_code_block(text: str) -> bool:
    return "```" in text


def _detect_table(text: str) -> bool:
    for line in text.split("\n"):
        if _TABLE_SEPARATOR_RE.match(line):
            return True
    return False


def _extract_lead_paragraph(plain_text: str) -> str:
    """첫 헤딩 이전의 본문 텍스트를 반환한다 (있으면)."""
    if not plain_text:
        return ""
    m = _HEADING_LINE_RE.search(plain_text)
    if m is None:
        return plain_text.strip()
    return plain_text[: m.start()].strip()


def _make_codec(
    model: str,
) -> tuple[Callable[[str], list[int]], Callable[[list[int]], str]]:
    """tiktoken encode/decode 함수 쌍을 반환한다. 없으면 문자 기반 폴백."""
    enc: Any = _get_tokenizer(model)
    if enc is not None:
        def encode(s: str) -> list[int]:
            return list(enc.encode(s))

        def decode(tokens: list[int]) -> str:
            return str(enc.decode(tokens))

        return encode, decode

    # 폴백: 1 char ≈ 1 token, 토큰 = 문자 코드 포인트 ord
    def fb_encode(s: str) -> list[int]:
        return [ord(c) for c in s]

    def fb_decode(tokens: list[int]) -> str:
        return "".join(chr(t) for t in tokens)

    return fb_encode, fb_decode


# ---------------------------------------------------------------------------
# sections 가 없는 문서 처리
# ---------------------------------------------------------------------------


def _build_from_plain_text(
    plain_text: str,
    *,
    document_id: int,
    doc_title: str,
    lead_paragraph: str,
    cfg: ExtractionUnitConfig,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> list[ExtractionUnit]:
    body = plain_text.strip()
    if not body:
        return []

    body_tokens = count_tokens(body, cfg.encoding_model)
    sid = _section_id(document_id, 0)

    if body_tokens <= cfg.max_tokens:
        pre_units = [_PreUnit(
            section_ids=(sid,),
            primary_section_id=sid,
            section_path=(),
            body=body,
            has_table=_detect_table(body),
            has_code_block=_detect_code_block(body),
        )]
    else:
        parts = _split_oversized(body, cfg=cfg, encode=encode, decode=decode)
        pre_units = [
            _PreUnit(
                section_ids=(sid,),
                primary_section_id=sid,
                section_path=(),
                body=part,
                has_table=_detect_table(part),
                has_code_block=_detect_code_block(part),
                split_part=i,
                split_total=len(parts),
            )
            for i, part in enumerate(parts)
        ]

    return _finalize(
        pre_units,
        document_id=document_id,
        doc_title=doc_title,
        lead_paragraph=lead_paragraph,
        cfg=cfg,
        encode=encode,
        decode=decode,
    )
