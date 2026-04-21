"""pipeline.process_document 테스트 — Confluence 추출기 + 링크 그래프.

검증 대상 동작:
  - Confluence(REST/MCP) + raw_content → extract()가 호출되고 extraction 메트릭
    이 반환 dict에 노출된다.
  - raw_content=None인 Confluence 문서는 추출기를 건너뛴다(extraction=None).
  - Confluence가 아닌 source_type(upload/manual)은 추출기·링크 그래프 모두 건
    너뛴다.
  - outbound_links가 있으면 build_link_graph로 그래프가 GraphStore에 저장되고
    link_node_count / link_edge_count 메트릭이 노출된다.
  - outbound_links가 비어 있으면 링크 그래프 저장은 호출되지 않는다.
  - 추출기가 만든 plain_text가 청커(chunk_text)에 전달된다.

벡터/그래프 호출은 mock으로 격리한다. LLM classifier / graph_extractor가 제거
되어 파이프라인에는 llm_client 파라미터가 없다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from context_loop.processor.pipeline import PipelineConfig, process_document
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


def _make_stores() -> tuple[MagicMock, MagicMock, MagicMock]:
    """파이프라인 외부 의존 객체들의 최소 mock 세트를 생성한다."""
    vector_store = MagicMock()
    vector_store.delete_by_document = MagicMock()
    vector_store.add_chunks = MagicMock()

    graph_store = MagicMock()
    graph_store.save_graph_data = AsyncMock(return_value={"nodes": 0, "edges": 0})

    embedding_client = MagicMock()
    embedding_client.aembed_documents = AsyncMock(return_value=[[0.1, 0.2]])
    return vector_store, graph_store, embedding_client


async def _create_confluence_doc(
    store: MetadataStore,
    *,
    raw_content: str | None,
    source_type: str = "confluence_mcp",
) -> int:
    return await store.create_document(
        source_type=source_type,
        source_id="p1",
        title="결제 시스템",
        original_content="# 결제 시스템\n본문",
        content_hash="h1",
        raw_content=raw_content,
    )


CONFLUENCE_HTML = """
<h1>결제 시스템</h1>
<p>인증은 <ac:link>
    <ri:page ri:content-title="인증 서비스" ri:space-key="ARCH"/>
    <ac:plain-text-link-body><![CDATA[인증 서비스]]></ac:plain-text-link-body>
</ac:link> 참고.</p>
<h2>엔드포인트</h2>
<table><tbody>
  <tr><th>Method</th><th>Path</th></tr>
  <tr><td>POST</td><td>/v1/payments</td></tr>
</tbody></table>
<ac:structured-macro ac:name="code">
  <ac:parameter ac:name="language">bash</ac:parameter>
  <ac:plain-text-body><![CDATA[curl -X POST /v1/payments]]></ac:plain-text-body>
</ac:structured-macro>
"""


@pytest.mark.asyncio
async def test_confluence_with_raw_content_runs_extractor(
    store: MetadataStore,
) -> None:
    """Confluence 문서 + raw_content가 있으면 extract()가 호출되고 메트릭이 노출된다."""
    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    assert result["extraction"] is not None
    assert result["extraction"]["sections"] >= 2  # 결제 시스템, 엔드포인트
    assert result["extraction"]["outbound_links"] >= 1
    assert result["extraction"]["code_blocks"] == 1
    assert result["extraction"]["tables"] == 1


@pytest.mark.asyncio
async def test_confluence_without_raw_content_skips_extractor(
    store: MetadataStore,
) -> None:
    """Confluence 문서라도 raw_content가 비어 있으면 추출기는 건너뛰고 extraction=None."""
    doc_id = await _create_confluence_doc(store, raw_content=None)
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=[],
    ), patch(
        "context_loop.processor.pipeline.extract_confluence",
    ) as mock_extract:
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    assert result["extraction"] is None
    mock_extract.assert_not_called()


@pytest.mark.asyncio
async def test_non_confluence_source_skips_extractor(store: MetadataStore) -> None:
    """upload/manual 등 다른 source_type은 raw_content가 있어도 추출기를 호출하지 않는다."""
    doc_id = await store.create_document(
        source_type="upload",
        title="문서",
        original_content="본문",
        content_hash="h",
        raw_content="<h1>ignored</h1>",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=[],
    ), patch(
        "context_loop.processor.pipeline.extract_confluence",
    ) as mock_extract:
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    assert result["extraction"] is None
    mock_extract.assert_not_called()


@pytest.mark.asyncio
async def test_confluence_rest_source_type_also_triggers_extractor(
    store: MetadataStore,
) -> None:
    """source_type='confluence'(REST 경로)도 동일하게 추출기가 호출된다."""
    doc_id = await _create_confluence_doc(
        store,
        raw_content="<h1>간단</h1><p>본문</p>",
        source_type="confluence",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    assert result["extraction"] is not None
    assert result["extraction"]["sections"] == 1


@pytest.mark.asyncio
async def test_link_graph_saved_for_confluence_with_outbound_links(
    store: MetadataStore,
) -> None:
    """Confluence 문서에 outbound_links가 있으면 링크 그래프가 GraphStore에 저장된다."""
    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    graph_store.save_graph_data = AsyncMock(return_value={
        "nodes": 2, "edges": 1, "merged": 0,
    })

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    # AST graph/LLM graph가 제거된 상태이므로 save_graph_data는 링크 그래프 1회만 호출된다.
    graph_store.save_graph_data.assert_awaited_once()
    saved_doc_id, saved_graph = graph_store.save_graph_data.await_args.args
    assert saved_doc_id == doc_id
    # CONFLUENCE_HTML은 page link 1개를 포함 → self + target 2 엔티티, 1 관계
    entity_types = {e.entity_type for e in saved_graph.entities}
    assert entity_types == {"document"}
    assert len(saved_graph.entities) == 2
    assert len(saved_graph.relations) == 1
    assert saved_graph.relations[0].relation_type == "references"

    assert result["link_node_count"] == 2
    assert result["link_edge_count"] == 1
    # 청크는 모두 mock되어 비어 있으므로 graph만 존재 → storage_method="graph"
    assert result["storage_method"] == "graph"


@pytest.mark.asyncio
async def test_link_graph_skipped_when_no_outbound_links(
    store: MetadataStore,
) -> None:
    """outbound_links가 비어 있으면 링크 그래프 save가 호출되지 않는다."""
    doc_id = await _create_confluence_doc(
        store,
        raw_content="<h1>제목</h1><p>링크 없는 본문</p>",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    graph_store.save_graph_data.assert_not_awaited()
    assert result["link_node_count"] == 0
    assert result["link_edge_count"] == 0


@pytest.mark.asyncio
async def test_link_graph_skipped_for_non_confluence_source(
    store: MetadataStore,
) -> None:
    """upload 등 Confluence가 아닌 소스는 추출기·링크 그래프 모두 건너뛴다."""
    doc_id = await store.create_document(
        source_type="upload",
        title="문서",
        original_content="본문",
        content_hash="h",
        raw_content="<h1>ignored</h1>",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    graph_store.save_graph_data.assert_not_awaited()
    assert result["link_node_count"] == 0
    assert result["link_edge_count"] == 0


@pytest.mark.asyncio
async def test_extracted_document_passed_to_structured_chunker(
    store: MetadataStore,
) -> None:
    """Confluence 경로에서는 chunk_extracted_document에 ExtractedDocument가 전달된다.

    원본 HTML이 추출기를 거쳐 나온 ``ExtractedDocument`` 가 그대로 청커에 전달
    되어야 하며, 생성된 청크에는 섹션 제목/앵커 메타가 실려야 한다.
    """
    doc_id = await _create_confluence_doc(
        store,
        raw_content="<h1>결제 시스템</h1><p>본문 텍스트</p>",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    captured: dict[str, Any] = {}

    def fake_chunker(
        extracted: Any, *, chunk_size: int, chunk_overlap: int, model: str,
    ) -> list[Any]:
        captured["extracted"] = extracted
        return []

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        side_effect=fake_chunker,
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    extracted = captured["extracted"]
    assert extracted is not None
    # 추출기는 h1 하나짜리 섹션을 만들어야 하고, 청커는 그 sections를 소비한다.
    assert len(extracted.sections) == 1
    assert extracted.sections[0].title == "결제 시스템"
    assert extracted.sections[0].level == 1
