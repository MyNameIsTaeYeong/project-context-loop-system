"""Extraction Unit 빌더 테스트."""

from __future__ import annotations

from context_loop.ingestion.confluence_extractor import ExtractedDocument, Section
from context_loop.processor.chunker import count_tokens
from context_loop.processor.extraction_unit import (
    ExtractionUnit,
    ExtractionUnitConfig,
    build_extraction_units,
)


def _section(level: int, title: str, body: str, path: list[str]) -> Section:
    return Section(
        level=level,
        title=title,
        anchor=title.lower().replace(" ", "-"),
        path=path,
        md_content=body,
    )


def _doc(
    sections: list[Section],
    *,
    plain_text: str = "",
) -> ExtractedDocument:
    return ExtractedDocument(plain_text=plain_text, sections=sections)


def _short() -> str:
    """짧은 본문(≤30 토큰)."""
    return "이것은 짧은 본문 텍스트입니다. 한두 문장 정도."


def _long(approx_tokens: int) -> str:
    """대략 ``approx_tokens`` 토큰인 본문을 생성한다."""
    word = "abcdef "
    chunks: list[str] = []
    for i in range(approx_tokens):
        chunks.append(f"{word}{i}")
    return " ".join(chunks)


# ---------------------------------------------------------------------------
# 기본 동작
# ---------------------------------------------------------------------------


def test_empty_document_returns_empty() -> None:
    units = build_extraction_units(
        ExtractedDocument(),
        document_id=1,
        doc_title="t",
    )
    assert units == []


def test_single_short_section_yields_one_unit() -> None:
    """얕은 트리에서 모든 섹션이 짧으면 1개 unit으로 응축된다."""
    sections = [
        _section(1, "Root", _short(), ["Root"]),
        _section(2, "A", _short(), ["Root", "A"]),
        _section(2, "B", _short(), ["Root", "B"]),
    ]
    cfg = ExtractionUnitConfig(target_tokens=500, max_tokens=1000, min_tokens=20)
    units = build_extraction_units(
        _doc(sections), document_id=42, doc_title="문서", config=cfg,
    )
    assert len(units) == 1
    u = units[0]
    assert u.unit_id == "42:0000"
    assert u.section_ids == ("42:0", "42:1", "42:2")
    assert u.primary_section_id == "42:0"
    assert u.section_path == ("Root",)
    assert u.split_part == 0 and u.split_total == 1
    assert "# Root" in u.body
    assert "## A" in u.body
    assert "## B" in u.body


def test_deeply_nested_mini_sections_condense_to_single_unit() -> None:
    """미니 H4/H5가 폭증해도 부모 H2 아래로 응축된다."""
    sections = [
        _section(2, "H2", _short(), ["H2"]),
        _section(3, "H3a", _short(), ["H2", "H3a"]),
        _section(4, "H4a", _short(), ["H2", "H3a", "H4a"]),
        _section(4, "H4b", _short(), ["H2", "H3a", "H4b"]),
        _section(3, "H3b", _short(), ["H2", "H3b"]),
        _section(4, "H4c", _short(), ["H2", "H3b", "H4c"]),
    ]
    cfg = ExtractionUnitConfig(target_tokens=1000, max_tokens=2000, min_tokens=20)
    units = build_extraction_units(_doc(sections), document_id=7, doc_title="d", config=cfg)
    assert len(units) == 1
    u = units[0]
    # 모든 섹션이 응축되어야 함 (section_ids 6개)
    assert u.section_ids == ("7:0", "7:1", "7:2", "7:3", "7:4", "7:5")
    # 본문에 모든 헤딩이 등장 (등장 순서 보존)
    body = u.body
    assert body.index("## H2") < body.index("### H3a") < body.index("#### H4a")
    assert body.index("#### H4a") < body.index("#### H4b") < body.index("### H3b")


# ---------------------------------------------------------------------------
# 분할
# ---------------------------------------------------------------------------


def test_oversized_single_section_splits_with_overlap() -> None:
    """own_tokens > max 인 섹션은 문단 경계에서 분할되며 overlap 이 적용된다."""
    paragraphs = [_long(40) for _ in range(10)]
    body = "\n\n".join(paragraphs)
    sections = [_section(1, "Big", body, ["Big"])]
    cfg = ExtractionUnitConfig(
        target_tokens=120,
        max_tokens=200,
        min_tokens=20,
        overlap_tokens=20,
    )
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="d", config=cfg,
    )
    assert len(units) >= 2
    # 모두 같은 섹션 출처
    for u in units:
        assert u.section_ids == ("1:0",)
        assert u.primary_section_id == "1:0"
        assert u.split_total == len(units)
    # split_part 가 0..N-1 순차
    assert [u.split_part for u in units] == list(range(len(units)))
    # 각 part 끝 일부 토큰이 다음 part 머리에 등장 (overlap)
    for i in range(len(units) - 1):
        prev_tail = units[i].body[-50:]  # 끝 50자
        # 끝 50자에서 단어 하나라도 다음 part 머리에 나타나면 overlap 성공
        next_head = units[i + 1].body[:200]
        # 단어 단위 매칭
        prev_words = [w for w in prev_tail.split() if w]
        assert any(w in next_head for w in prev_words[-3:]), (
            f"overlap 미적용: prev_tail={prev_tail!r}, next_head={next_head!r}"
        )


def test_atomic_code_block_not_split_even_when_oversized() -> None:
    """펜스 코드블록은 max_tokens 를 단독으로 초과해도 한 part 로 유지된다."""
    code_lines = "\n".join(f"line_{i} = compute({i})" for i in range(120))
    section_body = (
        "앞 설명 문단입니다.\n\n"
        f"```python\n{code_lines}\n```\n\n"
        "뒤 설명 문단입니다."
    )
    sections = [_section(1, "Code", section_body, ["Code"])]
    cfg = ExtractionUnitConfig(
        target_tokens=80,
        max_tokens=150,
        min_tokens=20,
        overlap_tokens=10,
    )
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="d", config=cfg,
    )
    # 코드블록은 어느 한 part 에 통째로 들어가야 함
    code_units = [u for u in units if "```python" in u.body]
    assert len(code_units) == 1
    assert "line_0 = compute(0)" in code_units[0].body
    assert "line_119 = compute(119)" in code_units[0].body
    assert code_units[0].has_code_block is True


def test_table_inline_in_unit_body() -> None:
    """마크다운 테이블이 같은 섹션의 설명문과 함께 한 unit에 포함된다."""
    section_body = (
        "다음은 의존성 표입니다.\n\n"
        "| 컴포넌트 | 종류 | 필수 |\n"
        "|---|---|---|\n"
        "| Token Validator | 내부 | Y |\n"
        "| User DB | PG | Y |\n\n"
        "표는 위와 같습니다."
    )
    sections = [_section(2, "Auth", section_body, ["Auth"])]
    cfg = ExtractionUnitConfig(target_tokens=500, max_tokens=1000, min_tokens=20)
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="d", config=cfg,
    )
    assert len(units) == 1
    u = units[0]
    assert u.has_table is True
    assert "| Token Validator | 내부 | Y |" in u.body
    assert "다음은 의존성 표입니다." in u.body
    assert "표는 위와 같습니다." in u.body


# ---------------------------------------------------------------------------
# 부모 own 흡수
# ---------------------------------------------------------------------------


def test_short_parent_body_absorbed_into_first_child() -> None:
    """부모 own_tokens < min_tokens 면 첫 자식 unit 머리로 흡수된다."""
    sections = [
        _section(1, "Parent", "짧은 부모 머리말.", ["Parent"]),   # ~10 토큰
        _section(2, "ChildA", _long(40), ["Parent", "ChildA"]),  # ~100 토큰
        _section(2, "ChildB", _long(40), ["Parent", "ChildB"]),  # ~100 토큰
    ]
    cfg = ExtractionUnitConfig(
        target_tokens=120,   # 자식 하나씩 condense, 둘이면 초과
        max_tokens=200,
        min_tokens=20,       # 부모 ~10 토큰 < 20 → 흡수 트리거
        overlap_tokens=10,
    )
    units = build_extraction_units(
        _doc(sections), document_id=9, doc_title="d", config=cfg,
    )
    assert len(units) >= 2
    first = units[0]
    # 첫 unit의 section_ids 가 부모 + 첫 자식 포함
    assert "9:0" in first.section_ids  # 부모
    assert "9:1" in first.section_ids  # 첫 자식
    # 부모 본문이 첫 unit body 머리에 prepend
    assert first.body.startswith("# Parent")
    assert "짧은 부모 머리말" in first.body
    # 두 번째 unit은 ChildB 만
    second = units[1]
    assert second.section_ids == ("9:2",)
    assert "## ChildB" in second.body
    assert "짧은 부모 머리말" not in second.body


def test_long_parent_body_emitted_as_standalone_unit() -> None:
    """부모 own_tokens >= min_tokens 면 단독 unit 으로 emit (자식과 분리)."""
    sections = [
        _section(1, "Parent", _long(40), ["Parent"]),         # ~100 토큰 own
        _section(2, "Child", _long(40), ["Parent", "Child"]), # ~100 토큰 own
    ]
    cfg = ExtractionUnitConfig(
        target_tokens=150,   # 부모+자식 합치면 200 > 150 → 응축 불가
        max_tokens=300,      # 부모/자식 각각 < max → 분할 X
        min_tokens=50,       # 부모 ~100 >= 50 → 흡수 X, 단독 emit
        overlap_tokens=10,
    )
    units = build_extraction_units(
        _doc(sections), document_id=3, doc_title="d", config=cfg,
    )
    assert len(units) == 2
    assert units[0].section_ids == ("3:0",)
    assert units[1].section_ids == ("3:1",)
    assert "# Parent" in units[0].body
    assert "## Child" in units[1].body


# ---------------------------------------------------------------------------
# breadcrumb / lead paragraph
# ---------------------------------------------------------------------------


def test_breadcrumb_includes_doc_title_and_path_by_default() -> None:
    sections = [_section(2, "Auth", _short(), ["Arch", "Auth"])]
    cfg = ExtractionUnitConfig(target_tokens=500, max_tokens=1000, min_tokens=20)
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="결제 플랫폼 설계서", config=cfg,
    )
    bc = units[0].breadcrumb
    assert "# 문서: 결제 플랫폼 설계서" in bc
    assert "## 위치: Arch > Auth" in bc
    # content 는 breadcrumb + separator + body 형태
    assert units[0].content.startswith("# 문서: 결제 플랫폼 설계서")
    assert "\n---\n" in units[0].content


def test_breadcrumb_all_toggles_off_yields_body_only_content() -> None:
    sections = [_section(1, "T", _short(), ["T"])]
    cfg = ExtractionUnitConfig(
        target_tokens=500,
        max_tokens=1000,
        min_tokens=20,
        breadcrumb_doc_title=False,
        breadcrumb_path=False,
        include_lead_paragraph=False,
    )
    units = build_extraction_units(
        _doc(sections, plain_text="머리말."), document_id=1,
        doc_title="t", config=cfg,
    )
    assert units[0].breadcrumb == ""
    assert units[0].content == units[0].body


def test_lead_paragraph_extracted_and_prefixed_to_all_units() -> None:
    """plain_text 의 첫 헤딩 이전 텍스트가 모든 unit 의 breadcrumb에 들어간다."""
    plain = (
        "이 문서는 결제 플랫폼의 전체 아키텍처를 다룹니다.\n"
        "도메인은 인증/결제/정산 3개입니다.\n\n"
        "# 1. 개요\n\n개요 본문."
    )
    sections = [
        _section(1, "1. 개요", "개요 본문.", ["1. 개요"]),
        _section(1, "2. 상세", _long(300), ["2. 상세"]),
    ]
    cfg = ExtractionUnitConfig(
        target_tokens=100,
        max_tokens=200,
        min_tokens=20,
        overlap_tokens=10,
        lead_paragraph_max_tokens=50,
    )
    units = build_extraction_units(
        _doc(sections, plain_text=plain), document_id=1, doc_title="d", config=cfg,
    )
    assert len(units) >= 2
    for u in units:
        assert "## 문서 요약" in u.breadcrumb
        assert "결제 플랫폼" in u.breadcrumb


def test_split_units_have_part_marker_in_breadcrumb() -> None:
    paragraphs = [_long(40) for _ in range(8)]
    body = "\n\n".join(paragraphs)
    sections = [_section(1, "Big", body, ["Big"])]
    cfg = ExtractionUnitConfig(
        target_tokens=120, max_tokens=200, min_tokens=20, overlap_tokens=10,
    )
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="d", config=cfg,
    )
    assert len(units) >= 2
    n = len(units)
    for i, u in enumerate(units):
        assert f"## 부분: {i + 1}/{n}" in u.breadcrumb


# ---------------------------------------------------------------------------
# sections 가 없는 문서
# ---------------------------------------------------------------------------


def test_no_sections_uses_plain_text_as_single_unit() -> None:
    text = _short() + "\n\n" + _short()
    units = build_extraction_units(
        _doc([], plain_text=text),
        document_id=1, doc_title="d",
    )
    assert len(units) == 1
    assert units[0].section_path == ()
    assert units[0].section_ids == ("1:0",)
    assert units[0].body.strip() == text.strip()


def test_no_sections_oversized_plain_text_splits() -> None:
    text = "\n\n".join(_long(40) for _ in range(10))
    cfg = ExtractionUnitConfig(
        target_tokens=120, max_tokens=200, min_tokens=20, overlap_tokens=10,
    )
    units = build_extraction_units(
        _doc([], plain_text=text),
        document_id=1, doc_title="d", config=cfg,
    )
    assert len(units) >= 2
    for u in units:
        assert u.section_ids == ("1:0",)
        assert u.split_total == len(units)


def test_no_sections_empty_plain_text_returns_empty() -> None:
    units = build_extraction_units(
        _doc([], plain_text="   "),
        document_id=1, doc_title="d",
    )
    assert units == []


# ---------------------------------------------------------------------------
# 메타: ordinal, unit_id, token_count
# ---------------------------------------------------------------------------


def test_ordinal_and_unit_id_are_sequential() -> None:
    sections = [
        _section(1, "A", _long(200), ["A"]),
        _section(1, "B", _long(200), ["B"]),
        _section(1, "C", _long(200), ["C"]),
    ]
    cfg = ExtractionUnitConfig(
        target_tokens=150, max_tokens=300, min_tokens=20,
    )
    units = build_extraction_units(
        _doc(sections), document_id=99, doc_title="d", config=cfg,
    )
    for i, u in enumerate(units):
        assert u.ordinal == i
        assert u.unit_id == f"99:{i:04d}"


def test_token_count_matches_content() -> None:
    sections = [_section(1, "T", _short(), ["T"])]
    cfg = ExtractionUnitConfig(target_tokens=500, max_tokens=1000, min_tokens=20)
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="d", config=cfg,
    )
    assert units[0].token_count == count_tokens(units[0].content, cfg.encoding_model)


def test_unit_dataclass_is_immutable() -> None:
    """ExtractionUnit 은 frozen dataclass 이다."""
    sections = [_section(1, "T", _short(), ["T"])]
    units = build_extraction_units(_doc(sections), document_id=1, doc_title="d")
    u = units[0]
    try:
        u.token_count = 0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ExtractionUnit must be frozen")


# ---------------------------------------------------------------------------
# 혼합 시나리오 (설계 문서의 워크스루 축소판)
# ---------------------------------------------------------------------------


def test_mixed_tree_condense_split_and_absorb() -> None:
    """짧은 형제는 응축 + 거대 섹션은 분할 + 짧은 부모는 흡수, 한 문서에서 동시 발생."""
    # 1. 개요 (짧음, 응축)
    # 2. 아키텍처 (부모 own 짧음 → 흡수)
    #    2.1 서비스 (자손 합 응축)
    #    2.2 데이터 흐름 (거대, 분할)
    sections = [
        _section(1, "1. 개요", _short(), ["1. 개요"]),
        _section(2, "1.1 목적", _short(), ["1. 개요", "1.1 목적"]),
        _section(1, "2. 아키텍처", "한 줄 머리말.", ["2. 아키텍처"]),
        _section(2, "2.1 서비스", _short(), ["2. 아키텍처", "2.1 서비스"]),
        _section(3, "2.1.1 Auth", _short(), ["2. 아키텍처", "2.1 서비스", "2.1.1 Auth"]),
        _section(2, "2.2 데이터 흐름",
                 "\n\n".join(_long(40) for _ in range(10)),
                 ["2. 아키텍처", "2.2 데이터 흐름"]),
    ]
    cfg = ExtractionUnitConfig(
        target_tokens=120,
        max_tokens=200,
        min_tokens=80,
        overlap_tokens=15,
    )
    units = build_extraction_units(
        _doc(sections), document_id=5, doc_title="설계서", config=cfg,
    )

    # 시나리오별 unit 분류
    overview = [u for u in units if "1. 개요" in u.section_path]
    arch_service = [u for u in units if u.section_path == ("2. 아키텍처", "2.1 서비스")]
    data_flow = [u for u in units if u.section_path == ("2. 아키텍처", "2.2 데이터 흐름")]

    # 1. 개요 + 1.1 목적 → 1 unit 으로 응축
    assert len(overview) == 1
    assert overview[0].section_ids == ("5:0", "5:1")

    # 2. 아키텍처 (own 짧음) 가 2.1 서비스 첫 unit 에 흡수됨
    assert len(arch_service) == 1
    assert "5:2" in arch_service[0].section_ids   # 부모
    assert "5:3" in arch_service[0].section_ids   # 2.1
    assert "5:4" in arch_service[0].section_ids   # 2.1.1
    assert "한 줄 머리말" in arch_service[0].body

    # 2.2 데이터 흐름 → 분할
    assert len(data_flow) >= 2
    n = len(data_flow)
    for i, u in enumerate(data_flow):
        assert u.section_ids == ("5:5",)
        assert u.split_part == i
        assert u.split_total == n

    # ordinal 은 전체 순서 보존
    for i, u in enumerate(units):
        assert u.ordinal == i


# ---------------------------------------------------------------------------
# 안정성
# ---------------------------------------------------------------------------


def test_section_with_empty_md_content_does_not_break_tree() -> None:
    """본문이 빈 헤딩 노드도 안전하게 처리된다."""
    sections = [
        _section(1, "Group", "", ["Group"]),
        _section(2, "Child", _short(), ["Group", "Child"]),
    ]
    cfg = ExtractionUnitConfig(target_tokens=500, max_tokens=1000, min_tokens=20)
    units = build_extraction_units(
        _doc(sections), document_id=1, doc_title="d", config=cfg,
    )
    assert len(units) == 1
    u = units[0]
    assert u.section_ids == ("1:0", "1:1")
    assert "# Group" in u.body
    assert "## Child" in u.body


def test_returned_units_are_extraction_unit_instances() -> None:
    sections = [_section(1, "T", _short(), ["T"])]
    units = build_extraction_units(_doc(sections), document_id=1, doc_title="d")
    assert len(units) == 1
    assert isinstance(units[0], ExtractionUnit)
