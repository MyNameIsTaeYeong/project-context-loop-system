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

from context_loop.processor.chunker import Chunk
from context_loop.processor.pipeline import (
    PipelineConfig,
    build_meta_view_text,
    process_document,
)
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

    async def _fake_embed(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]

    embedding_client.aembed_documents = AsyncMock(side_effect=_fake_embed)
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


@pytest.mark.asyncio
async def test_multi_view_embeddings_stored_for_chunks(
    store: MetadataStore,
) -> None:
    """D-042: 각 청크는 body + meta 두 벡터로 저장되고 같은 본문을 가리킨다.

    meta 뷰 텍스트는 ``title`` 과 ``section_path`` 를 결합하며, 논리 청크 ID는
    ChromaDB ID 접미사(``#body``/``#meta``)와 metadata.logical_chunk_id 로
    구분된다. 두 엔트리는 같은 본문(``document``) 문자열을 공유한다.
    SQLite chunks 테이블에는 여전히 논리 청크 1행만 저장된다.
    """
    doc_id = await _create_confluence_doc(
        store, raw_content="<h1>결제 시스템</h1><p>본문</p>",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    fake_chunks = [
        Chunk(
            id="c-abc",
            index=0,
            content="결제 본문 내용",
            token_count=5,
            section_path="결제 시스템",
            section_anchor="결제-시스템",
        ),
    ]

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        return_value=fake_chunks,
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    # body 텍스트 1건 + meta 텍스트 1건 = 2건 임베딩
    embed_call = embedding_client.aembed_documents.await_args
    assert embed_call is not None
    texts = embed_call.args[0]
    assert texts == ["결제 본문 내용", "결제 시스템\n결제 시스템"]

    # add_chunks 호출 검증
    vector_store.add_chunks.assert_called_once()
    ids, _embs, docs, metas = vector_store.add_chunks.call_args.args
    assert ids == ["c-abc#body", "c-abc#meta"]
    # 두 뷰가 같은 본문을 반환하도록 document 문자열이 동일해야 한다
    assert docs[0] == docs[1] == "결제 본문 내용"
    assert metas[0]["view"] == "body"
    assert metas[1]["view"] == "meta"
    assert metas[0]["logical_chunk_id"] == metas[1]["logical_chunk_id"] == "c-abc"
    assert metas[0]["section_anchor"] == "결제-시스템"

    # SQLite chunks는 여전히 논리 청크당 1행이며, section_path/anchor가 보존된다.
    stored = await store.get_chunks_by_document(doc_id)
    assert len(stored) == 1
    assert stored[0]["id"] == "c-abc"
    assert stored[0]["section_path"] == "결제 시스템"
    assert stored[0]["section_anchor"] == "결제-시스템"


@pytest.mark.asyncio
async def test_section_index_persisted_to_sqlite(
    store: MetadataStore,
) -> None:
    """청크의 ``section_index`` 가 SQLite chunks 테이블까지 전달되어 저장된다.

    PR-2: ExtractionUnit 의 ``section_ids`` 와 청크를 조인할 수 있게 하는
    안정적 출처 키. Confluence 구조화 추출 경로에서만 채워진다.
    """
    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()

    fake_chunks = [
        Chunk(
            id="c0", index=0, content="첫 섹션", token_count=3,
            section_path="결제 시스템", section_anchor="결제-시스템",
            section_index=0,
        ),
        Chunk(
            id="c1", index=1, content="둘째 섹션", token_count=3,
            section_path="결제 시스템 > 엔드포인트", section_anchor="엔드포인트",
            section_index=1,
        ),
    ]

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document",
        return_value=fake_chunks,
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    stored = await store.get_chunks_by_document(doc_id)
    by_id = {c["id"]: c for c in stored}
    assert by_id["c0"]["section_index"] == 0
    assert by_id["c1"]["section_index"] == 1


@pytest.mark.asyncio
async def test_section_index_null_for_chunks_without_section(
    store: MetadataStore,
) -> None:
    """section_index 가 ``None`` 인 청크는 SQLite 에 NULL 로 저장된다."""
    doc_id = await store.create_document(
        source_type="upload", title="t", original_content="x", content_hash="h",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    fake_chunks = [
        Chunk(id="c-null", index=0, content="본문", token_count=2),
    ]

    with patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=fake_chunks,
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    stored = await store.get_chunks_by_document(doc_id)
    assert len(stored) == 1
    assert stored[0]["section_index"] is None


@pytest.mark.asyncio
async def test_meta_view_skipped_when_title_and_path_empty(
    store: MetadataStore,
) -> None:
    """title과 section_path가 모두 비어 있으면 meta 뷰 엔트리를 생성하지 않는다."""
    doc_id = await store.create_document(
        source_type="upload",
        title="",
        original_content="some content",
        content_hash="h",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    fake_chunks = [
        Chunk(
            id="c-xyz",
            index=0,
            content="본문",
            token_count=2,
            section_path="",
            section_anchor="",
        ),
    ]

    with patch(
        "context_loop.processor.pipeline.chunk_text",
        return_value=fake_chunks,
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    ids, _embs, _docs, metas = vector_store.add_chunks.call_args.args
    assert ids == ["c-xyz#body"]
    assert metas[0]["view"] == "body"


def test_build_meta_view_text_combinations() -> None:
    """meta 뷰 텍스트는 title + section_path 결합. 결정론적 순수 함수.

    파이프라인 저장과 대시보드 청크 탭이 같은 함수를 호출하므로 두 곳에서
    동일한 값이 나와야 한다.
    """
    assert build_meta_view_text("배포 가이드", "배포 가이드 > 운영") == (
        "배포 가이드\n배포 가이드 > 운영"
    )
    assert build_meta_view_text("문서", "") == "문서"
    assert build_meta_view_text("", "경로") == "경로"
    assert build_meta_view_text("", "") == ""
    assert build_meta_view_text("  공백 제거  ", "  경로  ") == "공백 제거\n경로"


@pytest.mark.asyncio
async def test_git_code_pipeline_persists_embed_text(
    store: MetadataStore,
) -> None:
    """git_code 분기에서 embed_text(이름+시그니처+docstring)가 SQLite에 저장된다.

    임베딩 입력은 ``embed_texts``(생성된 단축 텍스트), 저장 본문은
    ``chunk.content``(전체 코드). 두 값이 다르므로 대시보드가 실제 임베딩
    대상을 보여주려면 embed_text가 영속화되어야 한다.
    """
    code = '''def hello(name: str) -> str:
    """Greet a person."""
    return f"Hello {name}"
'''
    doc_id = await store.create_document(
        source_type="git_code",
        source_id="src/greet.py",
        title="src/greet.py",
        original_content=code,
        content_hash="h-git",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    await process_document(
        doc_id,
        meta_store=store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embedding_client,
        config=PipelineConfig(),
    )

    # ChromaDB add_chunks: 청크당 1엔트리(멀티뷰 아님). 임베딩 입력은 본문이 아닌
    # 짧은 시그니처 텍스트 — 본문(전체 코드)보다 토큰이 적어야 한다.
    vector_store.add_chunks.assert_called_once()
    ids, _embs, docs, _metas = vector_store.add_chunks.call_args.args
    # #body/#meta 접미사가 붙지 않아야 함
    assert all("#" not in cid for cid in ids)
    # document(반환 본문)에는 함수 본문이 들어 있어야 함
    assert any("Hello {name}" in d for d in docs)

    # SQLite에 embed_text가 채워져 있어야 함
    stored = await store.get_chunks_by_document(doc_id)
    assert len(stored) == 1
    chunk = stored[0]
    assert chunk["embed_text"] != ""
    assert "hello" in chunk["embed_text"]  # 심볼 이름
    # 본문(전체 코드)와 다른 값이어야 함 — 임베딩과 저장의 분리(D-036)
    assert chunk["embed_text"] != chunk["content"]
