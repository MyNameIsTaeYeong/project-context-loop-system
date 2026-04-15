"""그래프 추출 모듈 테스트."""

from __future__ import annotations

import json

import pytest

from context_loop.processor.graph_extractor import (
    Entity,
    GraphData,
    Relation,
    _CODE_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    _merge_graphs,
    _select_prompts,
    _split_content,
    extract_graph,
)
from context_loop.processor.llm_client import LLMClient, extract_json


class MockLLMClient(LLMClient):
    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0
        self.last_system: str | None = None

    async def complete(self, prompt: str, *, system: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.0) -> str:
        self.call_count += 1
        self.last_system = system
        return self._response


class SequentialMockLLMClient(LLMClient):
    """호출마다 다른 응답을 반환하는 Mock LLM."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._index = 0
        self.call_count = 0

    async def complete(self, prompt: str, *, system: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.0) -> str:
        self.call_count += 1
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp
        return '{"entities": [], "relations": []}'


_VALID_RESPONSE = json.dumps({
    "entities": [
        {"name": "Auth Service", "type": "service", "description": "Authentication service"},
        {"name": "User DB", "type": "system", "description": "User database"},
        {"name": "API Gateway", "type": "component", "description": "API gateway"},
    ],
    "relations": [
        {"source": "API Gateway", "target": "Auth Service", "type": "depends_on", "label": "authenticates via"},
        {"source": "Auth Service", "target": "User DB", "type": "uses", "label": "queries"},
    ],
})


@pytest.mark.asyncio
async def test_extract_graph_success() -> None:
    """정상적인 응답에서 엔티티와 관계를 추출한다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Architecture", "System architecture doc")
    assert isinstance(result, GraphData)
    assert len(result.entities) == 3
    assert len(result.relations) == 2

    entity_names = [e.name for e in result.entities]
    assert "Auth Service" in entity_names
    assert "User DB" in entity_names


@pytest.mark.asyncio
async def test_extract_graph_entity_types() -> None:
    """엔티티 유형이 올바르게 파싱된다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Title", "content")
    auth = next(e for e in result.entities if e.name == "Auth Service")
    assert auth.entity_type == "service"
    assert auth.description == "Authentication service"


@pytest.mark.asyncio
async def test_extract_graph_relations() -> None:
    """관계가 올바르게 파싱된다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Title", "content")
    depends = next(r for r in result.relations if r.relation_type == "depends_on")
    assert depends.source == "API Gateway"
    assert depends.target == "Auth Service"
    assert depends.label == "authenticates via"


@pytest.mark.asyncio
async def test_extract_graph_fallback_on_invalid() -> None:
    """파싱 실패 시 빈 GraphData를 반환한다."""
    client = MockLLMClient("I cannot parse this")
    result = await extract_graph(client, "Title", "content")
    assert result.entities == []
    assert result.relations == []


@pytest.mark.asyncio
async def test_extract_graph_skips_missing_entities_in_relations() -> None:
    """relation의 source/target 엔티티가 없으면 relation이 무시되지 않는다 (추출 단계에서는 모두 포함)."""
    response = json.dumps({
        "entities": [{"name": "A", "type": "system"}],
        "relations": [
            {"source": "A", "target": "B", "type": "depends_on"},  # B는 엔티티 없음
        ],
    })
    client = MockLLMClient(response)
    result = await extract_graph(client, "Title", "content")
    # 추출 자체는 그대로 반환 (저장 시 필터링됨)
    assert len(result.relations) == 1


@pytest.mark.asyncio
async def test_extract_graph_truncated_json() -> None:
    """max_tokens 제한으로 잘린 JSON 응답에서도 완전한 항목을 추출한다."""
    truncated = (
        '{"entities": ['
        '{"name": "Auth Service", "type": "service", "description": "auth"},'
        '{"name": "User DB", "type": "system", "description": "db"}'
        '], "relations": ['
        '{"source": "Auth Service", "target": "User DB", "type": "uses", "label": "queries"},'
        '{"source": "Auth Service", "target": "Incom'  # 잘린 부분
    )
    client = MockLLMClient(truncated)
    result = await extract_graph(client, "Title", "content")
    assert len(result.entities) == 2
    assert len(result.relations) == 1
    assert result.relations[0].source == "Auth Service"


def test_extract_json_truncated_repair() -> None:
    """잘린 JSON 문자열을 복구하여 파싱한다."""
    truncated = '{"entities": [{"name": "A"}, {"name": "B"}], "relations": [{"source": "A", "targ'
    data = extract_json(truncated)
    assert len(data["entities"]) == 2
    # relations 키 자체가 잘려서 포함되지 않거나 빈 배열
    assert len(data.get("relations", [])) <= 1


def test_extract_json_truncated_code_block() -> None:
    """코드 블록 안의 잘린 JSON도 복구한다."""
    truncated = '```json\n{"entities": [{"name": "X"}], "relations": [{"sourc'
    data = extract_json(truncated)
    assert len(data["entities"]) == 1


# --- _split_content 단위 테스트 ---


def test_split_content_short_text() -> None:
    """max_chars 이하 텍스트는 단일 청크로 반환된다."""
    result = _split_content("짧은 텍스트", max_chars=100)
    assert result == ["짧은 텍스트"]


def test_split_content_paragraph_boundary() -> None:
    """단락 경계(\\n\\n)에서 분할한다."""
    content = "첫 번째 단락입니다." + "\n\n" + "두 번째 단락입니다."
    result = _split_content(content, max_chars=20)
    assert len(result) == 2
    assert "첫 번째" in result[0]
    assert "두 번째" in result[1]


def test_split_content_newline_boundary() -> None:
    """단락 경계가 없으면 줄바꿈에서 분할한다."""
    content = "라인1\n라인2\n라인3\n라인4\n라인5"
    result = _split_content(content, max_chars=12)
    assert len(result) >= 2
    # 모든 라인이 어딘가에 포함됨
    joined = "".join(result)
    assert "라인1" in joined
    assert "라인5" in joined


def test_split_content_no_boundary() -> None:
    """줄바꿈 없는 긴 텍스트는 강제 분할된다."""
    content = "가" * 100
    result = _split_content(content, max_chars=30)
    assert len(result) >= 3
    total_len = sum(len(c) for c in result)
    assert total_len == 100


def test_split_content_empty_chunks_filtered() -> None:
    """빈 청크는 결과에 포함되지 않는다."""
    content = "내용\n\n\n\n\n\n다음"
    result = _split_content(content, max_chars=10)
    for chunk in result:
        assert chunk.strip()


# --- _merge_graphs 단위 테스트 ---


def test_merge_graphs_deduplicates_entities() -> None:
    """동일 (name, type) 엔티티가 중복 제거된다."""
    g1 = GraphData(
        entities=[Entity(name="Auth", entity_type="service", description="인증")],
        relations=[],
    )
    g2 = GraphData(
        entities=[Entity(name="Auth", entity_type="service", description="인증 서비스")],
        relations=[],
    )
    merged = _merge_graphs([g1, g2])
    assert len(merged.entities) == 1
    # 먼저 나온 설명 유지
    assert merged.entities[0].description == "인증"


def test_merge_graphs_fills_missing_description() -> None:
    """기존 엔티티에 설명이 없으면 나중에 나온 설명으로 보충한다."""
    g1 = GraphData(
        entities=[Entity(name="DB", entity_type="system", description="")],
        relations=[],
    )
    g2 = GraphData(
        entities=[Entity(name="DB", entity_type="system", description="데이터베이스")],
        relations=[],
    )
    merged = _merge_graphs([g1, g2])
    assert len(merged.entities) == 1
    assert merged.entities[0].description == "데이터베이스"


def test_merge_graphs_deduplicates_relations() -> None:
    """동일 (source, target, type) 관계가 중복 제거된다."""
    rel = Relation(source="A", target="B", relation_type="uses")
    g1 = GraphData(entities=[], relations=[rel])
    g2 = GraphData(entities=[], relations=[rel])
    merged = _merge_graphs([g1, g2])
    assert len(merged.relations) == 1


def test_merge_graphs_case_insensitive() -> None:
    """엔티티/관계 중복 비교가 대소문자 무시한다."""
    g1 = GraphData(
        entities=[Entity(name="Auth Service", entity_type="service")],
        relations=[Relation(source="Auth Service", target="DB", relation_type="uses")],
    )
    g2 = GraphData(
        entities=[Entity(name="auth service", entity_type="service")],
        relations=[Relation(source="auth service", target="db", relation_type="uses")],
    )
    merged = _merge_graphs([g1, g2])
    assert len(merged.entities) == 1
    assert len(merged.relations) == 1


def test_merge_graphs_different_types_kept() -> None:
    """같은 이름이라도 entity_type이 다르면 별도 엔티티로 유지된다."""
    g1 = GraphData(
        entities=[Entity(name="Gateway", entity_type="service")],
        relations=[],
    )
    g2 = GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    )
    merged = _merge_graphs([g1, g2])
    assert len(merged.entities) == 2


def test_merge_graphs_empty() -> None:
    """빈 그래프 리스트를 병합하면 빈 결과를 반환한다."""
    merged = _merge_graphs([])
    assert merged.entities == []
    assert merged.relations == []


# --- Map-reduce 통합 테스트 ---


@pytest.mark.asyncio
async def test_extract_graph_short_document_single_call() -> None:
    """짧은 문서는 단일 LLM 호출로 처리된다."""
    client = MockLLMClient(_VALID_RESPONSE)
    content = "짧은 문서"  # 4000자 이하
    result = await extract_graph(client, "Title", content, max_content_chars=4000)
    assert client.call_count == 1
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_extract_graph_long_document_map_reduce() -> None:
    """긴 문서는 분할하여 여러 번 LLM을 호출하고 결과를 병합한다."""
    # 청크 1: Auth, Gateway
    resp1 = json.dumps({
        "entities": [
            {"name": "Auth", "type": "service", "description": "인증"},
            {"name": "Gateway", "type": "component", "description": "게이트웨이"},
        ],
        "relations": [
            {"source": "Gateway", "target": "Auth", "type": "depends_on"},
        ],
    })
    # 청크 2: Auth (중복), DB (신규)
    resp2 = json.dumps({
        "entities": [
            {"name": "Auth", "type": "service", "description": "인증 서비스"},
            {"name": "DB", "type": "system", "description": "데이터베이스"},
        ],
        "relations": [
            {"source": "Auth", "target": "DB", "type": "uses"},
        ],
    })
    client = SequentialMockLLMClient([resp1, resp2])

    # 100자씩 분할되도록 200자 이상 콘텐츠 생성
    content = ("문단 A입니다. " * 10 + "\n\n" + "문단 B입니다. " * 10)
    result = await extract_graph(client, "Title", content, max_content_chars=100)

    assert client.call_count >= 2
    # Auth 중복 제거 → 3개 엔티티
    entity_names = {e.name for e in result.entities}
    assert "Auth" in entity_names
    assert "Gateway" in entity_names
    assert "DB" in entity_names
    assert len(result.entities) == 3
    # 관계도 2개 (중복 없음)
    assert len(result.relations) == 2


@pytest.mark.asyncio
async def test_extract_graph_map_reduce_partial_failure() -> None:
    """map-reduce 중 일부 청크 실패 시 성공한 결과만 병합한다."""
    resp1 = json.dumps({
        "entities": [{"name": "ServiceA", "type": "service"}],
        "relations": [],
    })

    class FailSecondClient(LLMClient):
        def __init__(self) -> None:
            self.call_count = 0

        async def complete(self, prompt: str, *, system: str | None = None,
                           max_tokens: int = 1024, temperature: float = 0.0) -> str:
            self.call_count += 1
            if self.call_count == 2:
                raise Exception("LLM 서버 다운")
            return resp1

    client = FailSecondClient()
    content = "가" * 50 + "\n\n" + "나" * 50 + "\n\n" + "다" * 50
    result = await extract_graph(client, "Title", content, max_content_chars=60)

    # 2번째 청크 실패해도 나머지 결과 반환
    assert len(result.entities) >= 1
    assert result.entities[0].name == "ServiceA"


# --- 코드 전용 그래프 추출 테스트 ---


_CODE_RESPONSE = json.dumps({
    "entities": [
        {"name": "HandleRequest", "type": "function", "description": "HTTP request handler"},
        {"name": "VPCService", "type": "struct", "description": "VPC CRUD service"},
        {"name": "Repository", "type": "interface", "description": "Data access interface"},
    ],
    "relations": [
        {"source": "HandleRequest", "target": "VPCService", "type": "calls", "label": "invokes"},
        {"source": "VPCService", "target": "Repository", "type": "implements"},
    ],
})


def test_select_prompts_document() -> None:
    """source_type이 None이면 문서용 프롬프트를 반환한다."""
    sys_p, _, _ = _select_prompts(None)
    assert sys_p is _SYSTEM_PROMPT


def test_select_prompts_confluence() -> None:
    """source_type이 'confluence'이면 문서용 프롬프트를 반환한다."""
    sys_p, _, _ = _select_prompts("confluence")
    assert sys_p is _SYSTEM_PROMPT


def test_select_prompts_git_code() -> None:
    """source_type이 'git_code'이면 코드용 프롬프트를 반환한다."""
    sys_p, _, _ = _select_prompts("git_code")
    assert sys_p is _CODE_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_extract_graph_code_uses_code_prompt() -> None:
    """source_type='git_code'일 때 코드 전용 프롬프트가 사용된다."""
    client = MockLLMClient(_CODE_RESPONSE)
    result = await extract_graph(
        client, "handler.go", "package main\nfunc HandleRequest() {}",
        source_type="git_code",
    )
    assert client.last_system is _CODE_SYSTEM_PROMPT
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_extract_graph_document_uses_document_prompt() -> None:
    """source_type이 None이면 기존 문서 프롬프트가 사용된다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Architecture", "System architecture doc")
    assert client.last_system is _SYSTEM_PROMPT
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_extract_graph_code_entity_types() -> None:
    """코드 전용 엔티티 타입이 올바르게 파싱된다."""
    client = MockLLMClient(_CODE_RESPONSE)
    result = await extract_graph(
        client, "handler.go", "code",
        source_type="git_code",
    )
    types = {e.entity_type for e in result.entities}
    assert "function" in types
    assert "struct" in types
    assert "interface" in types


@pytest.mark.asyncio
async def test_extract_graph_code_relation_types() -> None:
    """코드 전용 관계 타입이 올바르게 파싱된다."""
    client = MockLLMClient(_CODE_RESPONSE)
    result = await extract_graph(
        client, "handler.go", "code",
        source_type="git_code",
    )
    rel_types = {r.relation_type for r in result.relations}
    assert "calls" in rel_types
    assert "implements" in rel_types


@pytest.mark.asyncio
async def test_extract_graph_code_map_reduce() -> None:
    """긴 코드 파일도 코드 전용 프롬프트로 map-reduce 처리된다."""
    resp1 = json.dumps({
        "entities": [
            {"name": "funcA", "type": "function"},
            {"name": "pkgX", "type": "package"},
        ],
        "relations": [
            {"source": "funcA", "target": "pkgX", "type": "imports"},
        ],
    })
    resp2 = json.dumps({
        "entities": [
            {"name": "funcB", "type": "function"},
            {"name": "pkgX", "type": "package"},  # 중복
        ],
        "relations": [
            {"source": "funcB", "target": "funcA", "type": "calls"},
        ],
    })

    class CapturingClient(LLMClient):
        def __init__(self) -> None:
            self.call_count = 0
            self.systems: list[str | None] = []

        async def complete(self, prompt: str, *, system: str | None = None,
                           max_tokens: int = 1024, temperature: float = 0.0) -> str:
            self.call_count += 1
            self.systems.append(system)
            return resp1 if self.call_count == 1 else resp2

    client = CapturingClient()
    content = "line " * 500 + "\n\n" + "code " * 500
    result = await extract_graph(
        client, "big_file.go", content,
        max_content_chars=100,
        source_type="git_code",
    )

    assert client.call_count >= 2
    # 모든 호출이 코드 프롬프트를 사용해야 함
    for sys in client.systems:
        assert sys is _CODE_SYSTEM_PROMPT
    # pkgX 중복 제거
    entity_names = {e.name for e in result.entities}
    assert "funcA" in entity_names
    assert "funcB" in entity_names
    assert "pkgX" in entity_names
    assert len([e for e in result.entities if e.name == "pkgX"]) == 1
