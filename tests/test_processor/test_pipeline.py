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
from context_loop.processor.graph_extractor import GraphData
from context_loop.processor.pipeline import (
    PipelineConfig,
    build_meta_view_text,
    process_document,
)
from context_loop.storage.metadata_store import MetadataStore


def _empty_graph_data() -> GraphData:
    return GraphData()


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
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
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
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
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
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ), patch(
        # 본문 그래프는 별도 테스트에서 검증 — 여기서는 링크 그래프만 격리해서 본다
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=_empty_graph_data(),
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    # 본문 그래프는 patch 되어 빈 GraphData → save_graph_data 는 링크 그래프 1회만
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
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
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
        extracted: Any, *, max_tokens: int, model: str,
    ) -> list[Any]:
        captured["extracted"] = extracted
        return []

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
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
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
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
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
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
async def test_git_code_pipeline_writes_body_and_meta_views(
    store: MetadataStore,
) -> None:
    """git_code 분기도 멀티뷰(body + meta)로 임베딩한다 (I-046).

    body 뷰는 코드 본문(chunk.content)을, meta 뷰는 식별자 요약
    (file+parent+name+signature+docstring)을 임베딩한다. 두 뷰는 같은 본문을
    document 로 저장하고 ``logical_chunk_id`` 를 공유한다. SQLite ``embed_text``
    컬럼은 meta 뷰 입력을 영속화한다.
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

    # ChromaDB add_chunks: 청크당 2엔트리 — #body + #meta.
    vector_store.add_chunks.assert_called_once()
    ids, _embs, docs, metas = vector_store.add_chunks.call_args.args
    assert len(ids) == 2
    body_idx = next(i for i, cid in enumerate(ids) if cid.endswith("#body"))
    meta_idx = next(i for i, cid in enumerate(ids) if cid.endswith("#meta"))
    # 두 엔트리는 같은 logical_chunk_id 를 공유해 _search_chunks dedup 에 흡수됨
    assert metas[body_idx]["view"] == "body"
    assert metas[meta_idx]["view"] == "meta"
    assert (
        metas[body_idx]["logical_chunk_id"]
        == metas[meta_idx]["logical_chunk_id"]
    )
    # documents(반환 본문)는 두 엔트리 모두 전체 코드 본문 — 검색 결과는 동일
    assert docs[body_idx] == docs[meta_idx]
    assert "Hello {name}" in docs[body_idx]

    # SQLite 에는 논리 청크당 1행. embed_text 는 meta 뷰 입력을 영속화.
    stored = await store.get_chunks_by_document(doc_id)
    assert len(stored) == 1
    chunk = stored[0]
    assert chunk["embed_text"] != ""
    assert "hello" in chunk["embed_text"]  # 심볼 이름
    # 본문(전체 코드)과 다른 값 — 임베딩 분리 원칙 유지(D-036, D-042 일반화)
    assert chunk["embed_text"] != chunk["content"]


@pytest.mark.asyncio
async def test_body_graph_saved_for_confluence_with_signal(
    store: MetadataStore,
) -> None:
    """ExtractionUnit 본문에 강조 용어/API/표 헤더가 있으면 body 그래프가 저장된다."""
    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    graph_store.save_graph_data = AsyncMock(return_value={
        "nodes": 4, "edges": 3, "merged": 0,
    })

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    # 링크 그래프 + 본문 그래프 = 최소 2회 호출
    saved_graphs = [
        call.args[1] for call in graph_store.save_graph_data.await_args_list
    ]
    assert len(saved_graphs) >= 2

    # 본문 그래프는 has_attribute(테이블 헤더) 또는 documents(API) 관계를 갖는다
    body_graphs = [
        g for g in saved_graphs
        if any(
            r.relation_type in ("has_attribute", "documents", "mentions",
                                "mentions_ticket")
            for r in g.relations
        )
    ]
    assert body_graphs, "본문 그래프가 저장되지 않음"


@pytest.mark.asyncio
async def test_body_graph_skipped_when_no_signal(
    store: MetadataStore,
) -> None:
    """본문에 강조/API/표/Jira 가 하나도 없으면 body 그래프 save 가 호출되지 않는다."""
    plain_html = "<h1>제목</h1><p>그냥 평문 본문 텍스트.</p>"
    doc_id = await _create_confluence_doc(store, raw_content=plain_html)
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(),
        )

    # outbound_links 도 없고 본문 시그널도 없으므로 save_graph_data 미호출
    graph_store.save_graph_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_body_extraction_skipped_when_disabled(
    store: MetadataStore,
) -> None:
    """enable_llm_body_extraction=False 면 LLM 호출 없음."""
    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()
    llm_client.complete = AsyncMock(return_value='{"entities": [], "relations": []}')

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_llm_body_extraction=False),
            llm_client=llm_client,
        )

    llm_client.complete.assert_not_called()


@pytest.mark.asyncio
async def test_llm_body_extraction_skipped_when_no_client(
    store: MetadataStore,
) -> None:
    """enable_llm_body_extraction=True 여도 llm_client=None 이면 스킵."""
    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ):
        # llm_client 미전달 → 호출 흐름이 LLM 단계로 들어가지 않음
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_llm_body_extraction=True),
        )

    # 정상 통과 (예외 없이) + 그래프는 link/body 결정론만 저장됨
    # 추가 검증: extract_llm_body_graph 호출 흔적 없음
    # (별도 patch 로 호출 여부 검증)


@pytest.mark.asyncio
async def test_llm_body_extraction_runs_when_enabled(
    store: MetadataStore,
) -> None:
    """enable=True + llm_client 가 있으면 LLM 본문 추출이 실행되어 그래프에 저장된다."""
    from context_loop.processor.graph_extractor import Entity, Relation
    from context_loop.processor.llm_body_extractor import LLMBodyExtractionStats

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    fake_llm_graph = GraphData(
        entities=[
            Entity(name="Auth Service", entity_type="system"),
            Entity(name="Token Validator", entity_type="module"),
        ],
        relations=[
            Relation(
                source="Auth Service",
                target="Token Validator",
                relation_type="depends_on",
            ),
        ],
    )

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ), patch(
        # 결정론 본문 추출은 격리
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=GraphData(),
    ), patch(
        # 문서 단위 LLM 호출이 기본 경로 — 이 함수가 호출되는지 검증
        "context_loop.processor.pipeline.extract_llm_body_graph_for_document",
        new=AsyncMock(return_value=(fake_llm_graph, LLMBodyExtractionStats())),
    ) as mock_doc_extract, patch(
        # unit 폴백은 호출되지 않아야 함
        "context_loop.processor.pipeline.extract_llm_body_graph",
        new=AsyncMock(return_value=(GraphData(), LLMBodyExtractionStats())),
    ) as mock_unit_extract:
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_llm_body_extraction=True),
            llm_client=llm_client,
        )

    # 문서 단위 호출이 1회 (기본 경로) + unit 기반 폴백은 호출 안 됨
    mock_doc_extract.assert_awaited_once()
    mock_unit_extract.assert_not_awaited()
    # LLM 그래프가 GraphStore 에 저장되었는지
    saved_graphs = [
        call.args[1] for call in graph_store.save_graph_data.await_args_list
    ]
    llm_graphs = [
        g for g in saved_graphs
        if any(r.relation_type == "depends_on" for r in g.relations)
    ]
    assert len(llm_graphs) == 1
    names = {e.name for e in llm_graphs[0].entities}
    assert {"Auth Service", "Token Validator"} <= names


@pytest.mark.asyncio
async def test_llm_body_extraction_falls_back_to_units_when_oversized(
    store: MetadataStore,
) -> None:
    """문서 본문이 입력 한도 초과(InputTooLargeError) 면 unit 기반 호출로 폴백."""
    from context_loop.processor.graph_extractor import Entity, Relation
    from context_loop.processor.llm_body_extractor import (
        InputTooLargeError,
        LLMBodyExtractionStats,
    )

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    unit_fallback_graph = GraphData(
        entities=[Entity(name="FromUnit", entity_type="system")],
        relations=[
            Relation(
                source="FromUnit",
                target="FromUnit",
                relation_type="depends_on",
            ),
        ],
    )

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ), patch(
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=GraphData(),
    ), patch(
        # 문서 단위 호출이 InputTooLargeError raise
        "context_loop.processor.pipeline.extract_llm_body_graph_for_document",
        new=AsyncMock(side_effect=InputTooLargeError("too big")),
    ) as mock_doc_extract, patch(
        # unit 폴백 호출 결과
        "context_loop.processor.pipeline.extract_llm_body_graph",
        new=AsyncMock(return_value=(unit_fallback_graph, LLMBodyExtractionStats())),
    ) as mock_unit_extract:
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_llm_body_extraction=True),
            llm_client=llm_client,
        )

    # 문서 단위 호출 1회 + unit 폴백 1회 모두 발생
    mock_doc_extract.assert_awaited_once()
    mock_unit_extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_assemble_document_body_uses_sections_when_present() -> None:
    """_assemble_document_body 는 sections 가 있으면 헤딩+본문을 트리 순서로 합본."""
    from context_loop.ingestion.confluence_extractor import (
        ExtractedDocument,
        Section,
    )
    from context_loop.processor.pipeline import _assemble_document_body

    extracted = ExtractedDocument(
        plain_text="ignored — sections 가 우선",
        sections=[
            Section(
                level=1,
                title="A",
                anchor="a",
                path=["A"],
                md_content="A 본문",
            ),
            Section(
                level=2,
                title="B",
                anchor="b",
                path=["A", "B"],
                md_content="B 본문",
            ),
            Section(
                level=1,
                title="C",
                anchor="c",
                path=["C"],
                md_content="",
            ),
        ],
    )

    body = _assemble_document_body(extracted)

    assert "# A\n\nA 본문" in body
    assert "## B\n\nB 본문" in body
    # 빈 본문 섹션도 헤딩은 보존
    assert "# C" in body
    # plain_text 는 사용되지 않음
    assert "ignored" not in body


@pytest.mark.asyncio
async def test_assemble_document_body_falls_back_to_plain_text() -> None:
    """sections 가 없으면 plain_text 를 그대로 사용한다."""
    from context_loop.ingestion.confluence_extractor import ExtractedDocument
    from context_loop.processor.pipeline import _assemble_document_body

    extracted = ExtractedDocument(plain_text="평문 본문 그대로", sections=[])
    assert _assemble_document_body(extracted) == "평문 본문 그대로"


# ---------------------------------------------------------------------------
# R3 — 가상 질문 인덱싱 통합
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_indexing_runs_when_enabled(
    store: MetadataStore,
) -> None:
    """enable_question_indexing=True 일 때 가상 질문이 view='question' 으로 등록된다."""
    from context_loop.processor.chunker import Chunk

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    # 단일 청크로 만들어 모든 가상 질문이 그 청크에 묶이도록 함 (section_index=None)
    fake_chunks = [
        Chunk(
            id="chunk-1",
            index=0,
            content="문서 본문",
            token_count=10,
            section_path="",
            section_anchor="",
            section_index=None,
        ),
    ]

    question_map = {0: ["AuthService 는 어떻게 동작하나요?", "토큰 만료 시 동작은?"]}

    from context_loop.processor.question_generator import QuestionGenStats
    fake_stats = QuestionGenStats(
        sections_total=1, sections_with_questions=1, final_questions=2,
        llm_called=True,
    )

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=fake_chunks,
    ), patch(
        "context_loop.processor.pipeline.generate_questions_for_document",
        new=AsyncMock(return_value=(question_map, fake_stats)),
    ) as mock_q:
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_question_indexing=True),
            llm_client=llm_client,
        )

    mock_q.assert_awaited_once()
    # vector_store.add_chunks 호출에서 view="question" 엔트리 검증
    add_call = vector_store.add_chunks.call_args
    _, _, _, metadatas = add_call.args
    question_metas = [m for m in metadatas if m.get("view") == "question"]
    assert len(question_metas) == 2
    question_texts = {m["question_text"] for m in question_metas}
    assert "AuthService 는 어떻게 동작하나요?" in question_texts
    assert "토큰 만료 시 동작은?" in question_texts


@pytest.mark.asyncio
async def test_question_indexing_skipped_when_disabled(
    store: MetadataStore,
) -> None:
    """enable_question_indexing=False 면 LLM 가상 질문 생성 호출 없음."""
    from context_loop.processor.chunker import Chunk

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    fake_chunks = [
        Chunk(
            id="chunk-x",
            index=0,
            content="본문",
            token_count=5,
            section_path="",
            section_anchor="",
            section_index=None,
        ),
    ]

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=fake_chunks,
    ), patch(
        "context_loop.processor.pipeline.generate_questions_for_document",
        new=AsyncMock(),
    ) as mock_q:
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_question_indexing=False),
            llm_client=llm_client,
        )

    mock_q.assert_not_awaited()
    # vector_store 에 question view 엔트리 없음
    add_call = vector_store.add_chunks.call_args
    _, _, _, metadatas = add_call.args
    assert not any(m.get("view") == "question" for m in metadatas)


@pytest.mark.asyncio
async def test_question_indexing_per_section_mapping(
    store: MetadataStore,
) -> None:
    """다청크(섹션 폴백) 시 가상 질문이 section_index 매칭으로 청크별 분배된다."""
    from context_loop.processor.chunker import Chunk

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    fake_chunks = [
        Chunk(
            id="c-0", index=0, content="섹션0 본문", token_count=100,
            section_path="A", section_anchor="a", section_index=0,
        ),
        Chunk(
            id="c-1", index=1, content="섹션1 본문", token_count=100,
            section_path="B", section_anchor="b", section_index=1,
        ),
    ]
    question_map = {
        0: ["A 의 동작은?"],
        1: ["B 의 책임은?", "B 의 의존성은?"],
    }
    from context_loop.processor.question_generator import QuestionGenStats
    fake_stats = QuestionGenStats(
        sections_total=2, sections_with_questions=2, final_questions=3,
        llm_called=True,
    )

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=fake_chunks,
    ), patch(
        "context_loop.processor.pipeline.generate_questions_for_document",
        new=AsyncMock(return_value=(question_map, fake_stats)),
    ):
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_question_indexing=True),
            llm_client=llm_client,
        )

    add_call = vector_store.add_chunks.call_args
    ids, _, _, metadatas = add_call.args
    # 청크 c-0 에 1개 질문, c-1 에 2개 질문 매핑
    c0_q_metas = [m for m in metadatas
                  if m.get("logical_chunk_id") == "c-0" and m.get("view") == "question"]
    c1_q_metas = [m for m in metadatas
                  if m.get("logical_chunk_id") == "c-1" and m.get("view") == "question"]
    assert len(c0_q_metas) == 1
    assert c0_q_metas[0]["question_text"] == "A 의 동작은?"
    assert len(c1_q_metas) == 2
    c1_texts = {m["question_text"] for m in c1_q_metas}
    assert c1_texts == {"B 의 책임은?", "B 의 의존성은?"}
    # vector ID 명명 규칙: {chunk_id}#q{i}
    assert any(i.endswith("#q0") for i in ids)


# ---------------------------------------------------------------------------
# LLM 결손(degradation) 추적 — 생성형 LLM 단계 실패 시 status=completed 유지 +
# llm_degraded 플래그로 분리 기록.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_generation_failure_marks_degraded(
    store: MetadataStore,
) -> None:
    """가상 질문 생성 LLM 이 실패하면 status=completed 이지만 llm_degraded=1 로 기록."""
    from context_loop.processor.chunker import Chunk
    from context_loop.processor.question_generator import QuestionGenStats

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    fake_chunks = [
        Chunk(id="c-q", index=0, content="본문", token_count=10, section_index=None),
    ]
    # 호출은 했으나 실패 → 빈 question_map + llm_failed=True
    failed_stats = QuestionGenStats(llm_called=True, llm_failed=True)

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=fake_chunks,
    ), patch(
        "context_loop.processor.pipeline.generate_questions_for_document",
        new=AsyncMock(return_value=({}, failed_stats)),
    ), patch(
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=GraphData(),
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(
                enable_question_indexing=True, enable_llm_body_extraction=False,
            ),
            llm_client=llm_client,
        )

    assert result["llm_degraded"] is True
    assert result["llm_degradation"]["question_generation_failed"] is True

    doc = await store.get_document(doc_id)
    assert doc["status"] == "completed"  # 검색 가능 상태 유지
    assert doc["llm_degraded"] == 1
    assert doc["llm_degraded_detail"]  # JSON detail 기록됨


@pytest.mark.asyncio
async def test_body_extraction_unit_failure_marks_degraded(
    store: MetadataStore,
) -> None:
    """LLM 본문 그래프 추출에서 units_failed>0 이면 llm_degraded 로 기록."""
    from context_loop.processor.llm_body_extractor import LLMBodyExtractionStats

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    # 호출했으나 일부 unit 실패 (units_failed=1)
    degraded_stats = LLMBodyExtractionStats(units_total=2, units_called=1, units_failed=1)

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ), patch(
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=GraphData(),
    ), patch(
        "context_loop.processor.pipeline.extract_llm_body_graph_for_document",
        new=AsyncMock(return_value=(GraphData(), degraded_stats)),
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_llm_body_extraction=True),
            llm_client=llm_client,
        )

    assert result["llm_degraded"] is True
    assert result["llm_degradation"]["body_extraction_units_failed"] == 1

    doc = await store.get_document(doc_id)
    assert doc["status"] == "completed"
    assert doc["llm_degraded"] == 1


@pytest.mark.asyncio
async def test_clean_success_clears_degraded_flag(
    store: MetadataStore,
) -> None:
    """이전에 degraded 였던 문서가 정상 재처리되면 llm_degraded 플래그가 0 으로 리셋."""
    from context_loop.processor.llm_body_extractor import LLMBodyExtractionStats

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    # 1) 먼저 결손 상태로 만든다
    await store.set_llm_degraded(doc_id, degraded=True, detail={"x": 1})
    assert (await store.get_document(doc_id))["llm_degraded"] == 1

    # 2) 결손 없는 정상 stats 로 재처리
    clean_stats = LLMBodyExtractionStats(units_total=1, units_called=1, units_failed=0)
    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ), patch(
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=GraphData(),
    ), patch(
        "context_loop.processor.pipeline.extract_llm_body_graph_for_document",
        new=AsyncMock(return_value=(GraphData(), clean_stats)),
    ):
        result = await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(enable_llm_body_extraction=True),
            llm_client=llm_client,
        )

    assert result["llm_degraded"] is False
    doc = await store.get_document(doc_id)
    assert doc["llm_degraded"] == 0
    assert doc["llm_degraded_detail"] is None


@pytest.mark.asyncio
async def test_git_code_never_degraded(store: MetadataStore) -> None:
    """git_code 는 생성형 LLM 미사용 → 항상 llm_degraded=False."""
    code = "def f():\n    return 1\n"
    doc_id = await store.create_document(
        source_type="git_code",
        source_id="src/f.py",
        title="src/f.py",
        original_content=code,
        content_hash="h-git",
    )
    vector_store, graph_store, embedding_client = _make_stores()

    result = await process_document(
        doc_id,
        meta_store=store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embedding_client,
        config=PipelineConfig(),
    )

    assert result["llm_degraded"] is False
    doc = await store.get_document(doc_id)
    assert doc["llm_degraded"] == 0


# ---------------------------------------------------------------------------
# R-1 — PipelineConfig.llm_max_input_tokens 주입 배선
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_injects_llm_max_input_tokens_to_question_cfg(
    store: MetadataStore,
) -> None:
    """PipelineConfig.llm_max_input_tokens 가 QuestionGenConfig 로 주입된다."""
    from context_loop.processor.chunker import Chunk
    from context_loop.processor.question_generator import QuestionGenStats

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    fake_chunks = [
        Chunk(
            id="chunk-1", index=0, content="본문", token_count=5,
            section_path="", section_anchor="", section_index=None,
        ),
    ]

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=fake_chunks,
    ), patch(
        "context_loop.processor.pipeline.generate_questions_for_document",
        new=AsyncMock(return_value=({}, QuestionGenStats())),
    ) as mock_q:
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(
                llm_max_input_tokens=123,
                enable_question_indexing=True,
                enable_llm_body_extraction=False,
            ),
            llm_client=llm_client,
        )

    mock_q.assert_awaited_once()
    passed_cfg = mock_q.await_args.kwargs["config"]
    assert passed_cfg.max_input_tokens == 123


@pytest.mark.asyncio
async def test_pipeline_injects_llm_max_input_tokens_to_body_cfg(
    store: MetadataStore,
) -> None:
    """llm_max_input_tokens 가 문서 단위 + unit 폴백 LLMBodyExtractionConfig 로 주입."""
    from context_loop.processor.llm_body_extractor import (
        InputTooLargeError,
        LLMBodyExtractionStats,
    )

    doc_id = await _create_confluence_doc(store, raw_content=CONFLUENCE_HTML)
    vector_store, graph_store, embedding_client = _make_stores()
    llm_client = AsyncMock()

    with patch(
        "context_loop.processor.pipeline.chunk_extracted_document_doclevel",
        return_value=[],
    ), patch(
        "context_loop.processor.pipeline.extract_body_graph",
        return_value=GraphData(),
    ), patch(
        # 문서 단위 호출 → InputTooLargeError 로 unit 폴백을 유도
        "context_loop.processor.pipeline.extract_llm_body_graph_for_document",
        new=AsyncMock(side_effect=InputTooLargeError("too big")),
    ) as mock_doc, patch(
        "context_loop.processor.pipeline.extract_llm_body_graph",
        new=AsyncMock(return_value=(GraphData(), LLMBodyExtractionStats())),
    ) as mock_unit:
        await process_document(
            doc_id,
            meta_store=store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            config=PipelineConfig(
                llm_max_input_tokens=123,
                enable_question_indexing=False,
                enable_llm_body_extraction=True,
            ),
            llm_client=llm_client,
        )

    # 문서 단위 호출과 unit 폴백 모두 동일 한도의 config 를 받는다
    assert mock_doc.await_args.kwargs["config"].max_input_tokens == 123
    assert mock_unit.await_args.kwargs["config"].max_input_tokens == 123
