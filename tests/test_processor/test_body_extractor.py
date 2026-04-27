"""ExtractionUnit 본문 결정론적 추출기 테스트."""

from __future__ import annotations

from context_loop.ingestion.confluence_extractor import ExtractedDocument, Section
from context_loop.processor.body_extractor import (
    BodyExtractionConfig,
    extract_body_graph,
)
from context_loop.processor.extraction_unit import (
    ExtractionUnit,
    build_extraction_units,
)


def _make_unit(
    *,
    body: str,
    section_path: tuple[str, ...] = ("Root",),
    document_id: int = 1,
    ordinal: int = 0,
) -> ExtractionUnit:
    """테스트용 ExtractionUnit 을 손으로 만든다 (build_extraction_units 우회)."""
    section_id = f"{document_id}:{ordinal}"
    return ExtractionUnit(
        unit_id=f"{document_id}:{ordinal:04d}",
        document_id=document_id,
        ordinal=ordinal,
        section_ids=(section_id,),
        primary_section_id=section_id,
        section_path=section_path,
        breadcrumb="",
        content=body,
        body=body,
        token_count=len(body),
        has_table="|---" in body,
        has_code_block="```" in body,
    )


def _section(level: int, title: str, body: str, path: list[str]) -> Section:
    return Section(
        level=level, title=title, anchor=title.lower().replace(" ", "-"),
        path=path, md_content=body,
    )


def _filter(graph, *, entity_type: str | None = None, relation_type: str | None = None):
    if entity_type is not None:
        return [e for e in graph.entities if e.entity_type == entity_type]
    if relation_type is not None:
        return [r for r in graph.relations if r.relation_type == relation_type]
    return []


# ---------------------------------------------------------------------------
# 빈 입력 / 가드
# ---------------------------------------------------------------------------


def test_empty_units_returns_empty_graph() -> None:
    g = extract_body_graph([], doc_title="d")
    assert g.entities == [] and g.relations == []


def test_empty_doc_title_returns_empty_graph() -> None:
    unit = _make_unit(body="**X**")
    g = extract_body_graph([unit], doc_title="")
    assert g.entities == [] and g.relations == []


def test_no_signal_in_body_returns_empty_graph() -> None:
    """추출 가능한 시그널이 하나도 없으면 self-entity 도 emit 하지 않는다."""
    unit = _make_unit(body="아무 의미 없는 평문 텍스트만 있음.")
    g = extract_body_graph([unit], doc_title="d")
    assert g.entities == [] and g.relations == []


# ---------------------------------------------------------------------------
# 굵게 강조 용어
# ---------------------------------------------------------------------------


def test_bold_terms_extracted_as_concept() -> None:
    body = (
        "본문에서 **Auth Service** 와 **Token Validator** 를 다룬다.\n"
        "또 다시 **Auth Service** 가 등장한다."
    )
    unit = _make_unit(body=body, section_path=("Arch", "Auth"))
    g = extract_body_graph([unit], doc_title="설계서")

    concepts = _filter(g, entity_type="concept")
    names = sorted(e.name for e in concepts)
    assert names == ["Auth Service", "Token Validator"]

    # self-entity 항상 첫 번째
    assert g.entities[0].name == "설계서"
    assert g.entities[0].entity_type == "document"

    # 모든 관계는 self-entity 에서 출발
    mentions = _filter(g, relation_type="mentions")
    assert {r.target for r in mentions} == {"Auth Service", "Token Validator"}
    assert all(r.source == "설계서" for r in mentions)
    # 첫 등장 unit 의 section_path 가 label 로 기록
    assert all(r.label == "Arch > Auth" for r in mentions)


def test_bold_alternative_underscore_syntax() -> None:
    body = "__Single Sign On__ 은 인증 표준."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    concepts = _filter(g, entity_type="concept")
    assert any(e.name == "Single Sign On" for e in concepts)


def test_bold_too_short_or_too_long_skipped() -> None:
    body = (
        "**A** 는 너무 짧음.\n\n"
        "**" + "이런 식으로 길게 풀어쓴 문장은 사실상 한 문단이라 개념으로 보기 "
        "어렵다 그래서 길이 상한을 둔다 정상 케이스가 아니다 무한히 길어지면 안된다 이런 거**\n\n"
        "**Valid Term** 정상."
    )
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    names = {e.name for e in _filter(g, entity_type="concept")}
    assert "Valid Term" in names
    assert "A" not in names
    # 긴 문장은 제외됨
    assert all(len(n) <= 60 for n in names)


def test_bold_inside_code_blocks_skipped() -> None:
    body = (
        "**Real Concept** 는 본문 강조.\n\n"
        "```python\n"
        "# **Fake Concept** 는 코드 주석이라 무시되어야 함\n"
        "x = 1\n"
        "```\n\n"
        "그리고 인라인 `**Inline Fake**` 도 무시.\n"
    )
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    names = {e.name for e in _filter(g, entity_type="concept")}
    assert "Real Concept" in names
    assert "Fake Concept" not in names
    assert "Inline Fake" not in names


def test_bold_extraction_can_be_disabled() -> None:
    body = "**X 라는 개념** 본문."
    unit = _make_unit(body=body)
    g = extract_body_graph(
        [unit], doc_title="d",
        config=BodyExtractionConfig(extract_bold_terms=False),
    )
    assert g.entities == [] and g.relations == []


def test_bold_numeric_only_skipped() -> None:
    """숫자/기호만 있는 강조는 의미 부족 → 제외."""
    body = "**123** 와 **!!** 는 의미 없음. **정상 용어** 만 남는다."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    names = {e.name for e in _filter(g, entity_type="concept")}
    assert "정상 용어" in names
    assert "123" not in names and "!!" not in names


# ---------------------------------------------------------------------------
# API 엔드포인트
# ---------------------------------------------------------------------------


def test_api_endpoints_extracted_from_code_blocks() -> None:
    body = (
        "사용 예:\n\n"
        "```bash\n"
        "curl -X POST /v1/payments\n"
        "curl -X GET /v1/payments/{id}\n"
        "```\n"
    )
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="결제")
    apis = _filter(g, entity_type="api")
    names = sorted(e.name for e in apis)
    assert names == ["GET /v1/payments/{id}", "POST /v1/payments"]

    docs_relations = _filter(g, relation_type="documents")
    assert {r.target for r in docs_relations} == set(names)


def test_api_endpoints_extracted_from_prose() -> None:
    body = "결제 호출은 POST /v1/payments 로 한다. 조회는 GET /v1/payments/123."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    names = {e.name for e in _filter(g, entity_type="api")}
    assert names == {"POST /v1/payments", "GET /v1/payments/123"}


def test_api_extraction_handles_quoted_paths() -> None:
    body = "엔드포인트는 `POST /v1/refund` 이며..."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    names = {e.name for e in _filter(g, entity_type="api")}
    assert "POST /v1/refund" in names


def test_method_alone_is_not_an_endpoint() -> None:
    body = "POST 는 HTTP method 이다. GET 도 마찬가지."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    assert _filter(g, entity_type="api") == []


# ---------------------------------------------------------------------------
# 표 헤더
# ---------------------------------------------------------------------------


def test_table_headers_extracted_as_concept() -> None:
    body = (
        "의존성 표:\n\n"
        "| 컴포넌트 | 종류 | 필수 |\n"
        "|---|---|---|\n"
        "| Token Validator | 내부 | Y |\n"
        "| User DB | PG | Y |\n"
    )
    unit = _make_unit(body=body, section_path=("Arch",))
    g = extract_body_graph([unit], doc_title="d")
    headers = {e.name for e in _filter(g, entity_type="concept")}
    assert {"컴포넌트", "종류", "필수"} <= headers

    has_attr = _filter(g, relation_type="has_attribute")
    assert {"컴포넌트", "종류", "필수"} <= {r.target for r in has_attr}
    assert all(r.label == "Arch" for r in has_attr)


def test_huge_tables_are_skipped() -> None:
    """열 수가 ``max_table_columns`` 초과면 표를 건너뛴다."""
    headers = "| " + " | ".join(f"col{i}" for i in range(15)) + " |"
    sep = "|" + "---|" * 15
    row = "| " + " | ".join(f"v{i}" for i in range(15)) + " |"
    body = headers + "\n" + sep + "\n" + row
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    assert _filter(g, entity_type="concept") == []


def test_table_extraction_can_be_disabled() -> None:
    body = (
        "| H1 | H2 |\n"
        "|---|---|\n"
        "| a | b |\n"
    )
    unit = _make_unit(body=body)
    g = extract_body_graph(
        [unit], doc_title="d",
        config=BodyExtractionConfig(extract_table_headers=False),
    )
    assert _filter(g, entity_type="concept") == []


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


def test_jira_keys_extracted_as_ticket() -> None:
    body = "관련: PROJ-123, BUG-7. 본문에서 PROJ-123 다시 언급."
    unit = _make_unit(body=body, section_path=("Notes",))
    g = extract_body_graph([unit], doc_title="d")
    tickets = sorted(e.name for e in _filter(g, entity_type="ticket"))
    assert tickets == ["BUG-7", "PROJ-123"]
    rels = _filter(g, relation_type="mentions_ticket")
    assert {r.target for r in rels} == {"BUG-7", "PROJ-123"}
    assert all(r.label == "Notes" for r in rels)


def test_jira_keys_inside_code_blocks_skipped() -> None:
    body = (
        "본문 PROJ-1 등장.\n\n"
        "```\n"
        "// FAKE-99 는 코드 주석이라 패스\n"
        "```\n"
    )
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    tickets = {e.name for e in _filter(g, entity_type="ticket")}
    assert "PROJ-1" in tickets
    assert "FAKE-99" not in tickets


def test_lowercase_jira_keys_not_matched() -> None:
    body = "abc-123 는 Jira 키가 아님."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    assert _filter(g, entity_type="ticket") == []


# ---------------------------------------------------------------------------
# 통합 / 정규화 / 누적
# ---------------------------------------------------------------------------


def test_cross_unit_entity_dedup() -> None:
    """여러 unit 에 같은 용어가 등장해도 entity / relation 는 한 개."""
    u1 = _make_unit(body="**Auth Service** 는 1번 unit.", section_path=("S1",), ordinal=0)
    u2 = _make_unit(body="**Auth Service** 는 2번 unit.", section_path=("S2",), ordinal=1)
    g = extract_body_graph([u1, u2], doc_title="d")
    concepts = _filter(g, entity_type="concept")
    assert [e.name for e in concepts] == ["Auth Service"]
    rels = _filter(g, relation_type="mentions")
    assert len(rels) == 1
    # label 은 첫 등장 unit (S1) 의 section_path
    assert rels[0].label == "S1"


def test_case_insensitive_entity_dedup_keeps_first_casing() -> None:
    body = "처음 **Auth Service**, 그 다음 **AUTH SERVICE** 도 등장."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="d")
    concepts = _filter(g, entity_type="concept")
    assert len(concepts) == 1
    # 첫 등장의 표기를 보존
    assert concepts[0].name == "Auth Service"


def test_self_entity_first_and_only_one() -> None:
    body = "**A** 와 **B** 그리고 POST /x 와 PROJ-1."
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="설계서")
    docs = [e for e in g.entities if e.entity_type == "document"]
    assert len(docs) == 1
    assert g.entities[0].name == "설계서"


def test_multiple_signal_types_in_one_unit() -> None:
    body = (
        "## Auth Service\n\n"
        "**Auth Service** 는 인증을 담당한다. 관련 티켓 PROJ-42.\n\n"
        "예시 호출:\n\n"
        "```bash\n"
        "POST /v1/auth/login\n"
        "```\n\n"
        "| 항목 | 값 |\n"
        "|---|---|\n"
        "| 의존 | Token Validator |\n"
    )
    unit = _make_unit(body=body)
    g = extract_body_graph([unit], doc_title="설계")
    types = {(e.name, e.entity_type) for e in g.entities}
    assert ("Auth Service", "concept") in types
    assert ("PROJ-42", "ticket") in types
    assert ("POST /v1/auth/login", "api") in types
    assert ("항목", "concept") in types or ("값", "concept") in types
    rel_types = {r.relation_type for r in g.relations}
    assert {"mentions", "documents", "has_attribute", "mentions_ticket"} <= rel_types


def test_integration_with_build_extraction_units() -> None:
    """실제 build_extraction_units 결과를 그대로 추출기에 넣어도 동작한다."""
    sections = [
        _section(1, "Auth", "**Auth Service** 본문.", ["Auth"]),
        _section(2, "API", "POST /v1/login 호출.", ["Auth", "API"]),
    ]
    units = build_extraction_units(
        ExtractedDocument(plain_text="ignored", sections=sections),
        document_id=1, doc_title="d",
    )
    g = extract_body_graph(units, doc_title="d")
    names = {e.name for e in g.entities}
    assert "Auth Service" in names
    assert "POST /v1/login" in names
    # self-entity 도 포함
    assert "d" in names
