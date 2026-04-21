"""pipeline.process_document 테스트 — Confluence 추출기 주입 위주.

Step 1.5에서 추가된 아래 동작을 검증한다:
  - source_type이 confluence/confluence_mcp이고 raw_content가 있으면
    confluence_extractor.extract()가 호출되어 반환 dict에 extraction 메트릭이 노출됨
  - raw_content가 None인 Confluence 문서는 기존 경로(original_content만 사용) 유지
  - 다른 source_type(manual/upload)은 추출기 미호출

파이프라인의 LLM/벡터/그래프 호출은 mock으로 대체해 격리한다.
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


def _make_stores() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """파이프라인 외부 의존 객체들의 최소 mock 세트를 생성한다."""
    vector_store = MagicMock()
    vector_store.delete_by_document = MagicMock()
    vector_store.add_chunks = MagicMock()

    graph_store = MagicMock()
    graph_store.save_graph_data = AsyncMock(return_value={"nodes": 0, "edges": 0})

    llm_client = MagicMock()
    embedding_client = MagicMock()
    embedding_client.aembed_documents = AsyncMock(return_value=[[0.1, 0.2]])
    return vector_store, graph_store, llm_client, embedding_client


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
    vector_store, graph_store, llm_client, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=("chunk", "테스트"),
    ), patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            llm_client=llm_client,
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
    vector_store, graph_store, llm_client, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=("chunk", "테스트"),
    ), patch(
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
            llm_client=llm_client,
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
    vector_store, graph_store, llm_client, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=("chunk", "테스트"),
    ), patch(
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
            llm_client=llm_client,
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
    vector_store, graph_store, llm_client, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.classify_document",
        new_callable=AsyncMock,
        return_value=("chunk", "테스트"),
    ), patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=[],
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            llm_client=llm_client,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    assert result["extraction"] is not None
    assert result["extraction"]["sections"] == 1


@pytest.mark.asyncio
async def test_extractor_output_replaces_content_for_classifier(
    store: MetadataStore,
) -> None:
    """추출기가 실행되면 classifier에 전달되는 content가 plain_text로 교체된다.

    original_content에는 "본문"만 있지만 추출기는 HTML을 마크다운으로 변환해
    헤딩 마크업 등이 포함된 더 풍부한 텍스트를 만든다. 이 텍스트가 classifier에
    전달되는지 검증한다.
    """
    doc_id = await _create_confluence_doc(
        store,
        raw_content="<h1>결제 시스템</h1><p>본문 텍스트</p>",
    )
    vector_store, graph_store, llm_client, embedding_client = _make_stores()

    captured: dict[str, Any] = {}

    async def fake_classify(_llm: Any, title: str, content: str) -> tuple[str, str]:
        captured["title"] = title
        captured["content"] = content
        return "chunk", "테스트"

    with patch(
        "context_loop.processor.pipeline.classify_document",
        side_effect=fake_classify,
    ), patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=[],
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            llm_client=llm_client,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    # 추출기 결과가 classifier에 전달됨: 마크다운 헤딩 "# 결제 시스템"이 포함되어야 함
    assert "# 결제 시스템" in captured["content"]
