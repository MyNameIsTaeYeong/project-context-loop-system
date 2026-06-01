"""문서 API 테스트."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_document(client, stores):
    """에디터에서 문서를 생성하면 HX-Redirect가 반환된다."""
    resp = await client.post(
        "/api/documents",
        data={"title": "Test Doc", "content": "Hello World"},
    )
    assert resp.status_code == 204
    assert "/documents/" in resp.headers.get("hx-redirect", "")


@pytest.mark.asyncio
async def test_create_and_view_document(client, stores):
    """문서를 생성하고 상세 페이지를 조회한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Test",
        original_content="# Hello",
        content_hash="abc123",
    )

    resp = await client.get(f"/documents/{doc_id}")
    assert resp.status_code == 200
    assert "Test" in resp.text


@pytest.mark.asyncio
async def test_document_original_tab(client, stores):
    """원본 탭 파셜이 문서 내용을 반환한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Tab Test",
        original_content="Content here",
        content_hash="h1",
    )

    resp = await client.get(f"/partials/document/{doc_id}/original")
    assert resp.status_code == 200
    assert "Content here" in resp.text


@pytest.mark.asyncio
async def test_document_original_tab_renders_markdown_for_confluence(client, stores):
    """confluence_mcp 등 마크다운 소스는 Rendered/Raw 토글과 data-markdown
    컨테이너가 노출되어야 한다. 클라이언트 측 marked.js 가 .md-source 의
    원본 텍스트를 읽어 HTML 로 변환한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="confluence_mcp",
        title="MD Doc",
        original_content="# Title\n\nHello **world**",
        content_hash="hmd",
    )

    resp = await client.get(f"/partials/document/{doc_id}/original")
    assert resp.status_code == 200
    # Rendered/Raw 토글 버튼이 존재
    assert ">Rendered<" in resp.text
    assert ">Raw<" in resp.text
    # 클라이언트 렌더용 컨테이너와 원본 소스 엘리먼트
    assert "data-markdown" in resp.text
    assert f'id="md-source-original-{doc_id}"' in resp.text
    assert 'class="md-source"' in resp.text
    # 원본 마크다운 문자열은 그대로 페이지에 포함되어야 한다
    assert "# Title" in resp.text
    assert "Hello **world**" in resp.text


@pytest.mark.asyncio
async def test_document_original_tab_falls_back_to_raw_content(client, stores):
    """original_content(마크다운)가 비어 있고 raw_content(원본 HTML)가 있으면
    원본 HTML을 렌더 가능한 표준 HTML로 전처리해 폴백 표시한다. 큰/중첩 깊은
    Confluence 문서에서 HTML→마크다운 변환이 실패해 original_content 가 빈
    경우의 복구 경로. 클라이언트가 DOMPurify 로 sanitize 후 렌더한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="confluence_mcp",
        title="Big Doc",
        original_content="",
        raw_content=(
            "<h1>원본 제목</h1>"
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            '<ac:plain-text-body><![CDATA[print("hi")]]></ac:plain-text-body>'
            "</ac:structured-macro>"
        ),
        content_hash="hraw",
    )

    resp = await client.get(f"/partials/document/{doc_id}/original")
    assert resp.status_code == 200
    # 폴백 안내 문구 노출
    assert "원본 HTML" in resp.text
    # 클라이언트 HTML 렌더용 컨테이너와 소스 엘리먼트
    assert "data-html" in resp.text
    assert f'id="html-source-original-{doc_id}"' in resp.text
    assert 'class="html-source"' in resp.text
    # Confluence 매크로가 표준 HTML(code 블록)로 전처리되어 소스에 포함
    assert "language-python" in resp.text
    # 마크다운 렌더 컨테이너(md-source)는 없어야 한다
    assert "md-source-original" not in resp.text
    # 빈 본문 안내가 폴백을 가리지 않아야 한다
    assert "(no content)" not in resp.text


@pytest.mark.asyncio
async def test_document_original_tab_no_content_when_both_empty(client, stores):
    """original_content 와 raw_content 가 모두 비면 '(no content)' 를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="confluence_mcp",
        title="Empty Doc",
        original_content="",
        raw_content=None,
        content_hash="hempty",
    )

    resp = await client.get(f"/partials/document/{doc_id}/original")
    assert resp.status_code == 200
    assert "(no content)" in resp.text


@pytest.mark.asyncio
async def test_document_original_tab_git_code_no_markdown(client, stores):
    """git_code 소스는 코드이므로 마크다운 렌더링 컨테이너가 없어야 한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="git_code",
        title="src/example.py",
        source_id="src/example.py",
        original_content="def foo():\n    return 1\n",
        content_hash="hcode",
    )

    resp = await client.get(f"/partials/document/{doc_id}/original")
    assert resp.status_code == 200
    assert "data-markdown" not in resp.text
    assert "md-source-original" not in resp.text
    # 코드는 syntax highlight 로만 노출
    assert "hljs" in resp.text or "highlight" in resp.text


@pytest.mark.asyncio
async def test_document_chunks_tab_renders_markdown_body(client, stores):
    """non-git_code 청크의 body 는 마크다운 토글/컨테이너가 노출되어
    클라이언트에서 marked.js 로 렌더링될 수 있어야 한다."""
    meta_store, _vector_store, _ = stores

    doc_id = await meta_store.create_document(
        source_type="confluence_mcp",
        title="ChunkMD",
        original_content="본문",
        content_hash="hcmd",
    )
    await meta_store.create_chunk(
        chunk_id="md-chunk-1",
        document_id=doc_id,
        chunk_index=0,
        content="## Sub\n\n- item1\n- item2",
        token_count=12,
        section_path="",
        section_anchor="",
    )

    resp = await client.get(f"/partials/document/{doc_id}/chunks")
    assert resp.status_code == 200
    # 청크별 Rendered/Raw 토글 + 마크다운 컨테이너
    assert ">Rendered<" in resp.text
    assert ">Raw<" in resp.text
    assert 'id="md-source-chunk-md-chunk-1"' in resp.text
    assert "data-markdown" in resp.text
    # 원본 청크 텍스트도 그대로 포함
    assert "## Sub" in resp.text


@pytest.mark.asyncio
async def test_document_chunks_tab_git_code_no_markdown(client, stores):
    """git_code 청크는 코드 본문이므로 마크다운 토글이 없어야 한다."""
    meta_store, _vector_store, _ = stores

    doc_id = await meta_store.create_document(
        source_type="git_code",
        title="src/x.py",
        source_id="src/x.py",
        original_content="x = 1\n",
        content_hash="hgc",
    )
    await meta_store.create_chunk(
        chunk_id="code-chunk-1",
        document_id=doc_id,
        chunk_index=0,
        content="def foo():\n    return 1",
        token_count=8,
        section_path="",
        section_anchor="",
    )

    resp = await client.get(f"/partials/document/{doc_id}/chunks")
    assert resp.status_code == 200
    assert "data-markdown" not in resp.text
    assert "md-source-chunk" not in resp.text


@pytest.mark.asyncio
async def test_document_chunks_tab_empty(client, stores):
    """청크가 없는 문서의 청크 탭은 안내 메시지를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="No Chunks",
        original_content="Text",
        content_hash="h2",
    )

    resp = await client.get(f"/partials/document/{doc_id}/chunks")
    assert resp.status_code == 200
    assert "No chunks" in resp.text


@pytest.mark.asyncio
async def test_document_chunks_tab_shows_virtual_questions(client, stores):
    """R3 — 청크 탭이 vector_store 의 view='question' 엔트리를 청크별로 표시.

    SQLite chunks 테이블에는 본문 청크만 있고, 가상 질문은 vector_store 가
    단일 진실의 원천. logical_chunk_id 로 조인하여 UI 에 노출된다.
    """
    meta_store, vector_store, _ = stores

    doc_id = await meta_store.create_document(
        source_type="confluence_mcp",
        title="QDoc",
        original_content="본문",
        content_hash="hq1",
    )
    await meta_store.create_chunk(
        chunk_id="qchunk-1",
        document_id=doc_id,
        chunk_index=0,
        content="본문 내용",
        token_count=10,
        section_path="",
        section_anchor="",
    )
    # body + 가상 질문 2개를 vector_store 에 등록 (R3 파이프라인이 만드는 형태).
    vector_store.add_chunks(
        chunk_ids=["qchunk-1#body", "qchunk-1#q0", "qchunk-1#q1"],
        embeddings=[[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
        documents=["본문 내용", "본문 내용", "본문 내용"],
        metadatas=[
            {"document_id": doc_id, "logical_chunk_id": "qchunk-1",
             "chunk_index": 0, "view": "body"},
            {"document_id": doc_id, "logical_chunk_id": "qchunk-1",
             "chunk_index": 0, "view": "question",
             "question_text": "QDoc 의 핵심 동작은?"},
            {"document_id": doc_id, "logical_chunk_id": "qchunk-1",
             "chunk_index": 0, "view": "question",
             "question_text": "QDoc 의 의존성은 무엇인가?"},
        ],
    )

    resp = await client.get(f"/partials/document/{doc_id}/chunks")
    assert resp.status_code == 200
    # 헤더에 가상 질문 개수 배지
    assert "+ 2 가상 질문" in resp.text
    # 질문 본문 노출
    assert "QDoc 의 핵심 동작은?" in resp.text
    assert "QDoc 의 의존성은 무엇인가?" in resp.text


@pytest.mark.asyncio
async def test_document_chunks_tab_no_questions_for_legacy_chunk(client, stores):
    """가상 질문이 없는 (구버전) 청크에는 질문 섹션이 표시되지 않는다."""
    meta_store, vector_store, _ = stores

    doc_id = await meta_store.create_document(
        source_type="confluence_mcp",
        title="LegacyDoc",
        original_content="레거시",
        content_hash="hleg",
    )
    await meta_store.create_chunk(
        chunk_id="legacy-1",
        document_id=doc_id,
        chunk_index=0,
        content="레거시 본문",
        token_count=8,
        section_path="",
        section_anchor="",
    )
    # body 만 등록 (question view 없음 — 구버전 인덱싱)
    vector_store.add_chunks(
        chunk_ids=["legacy-1#body"],
        embeddings=[[0.1, 0.2]],
        documents=["레거시 본문"],
        metadatas=[
            {"document_id": doc_id, "logical_chunk_id": "legacy-1",
             "chunk_index": 0, "view": "body"},
        ],
    )

    resp = await client.get(f"/partials/document/{doc_id}/chunks")
    assert resp.status_code == 200
    # 가상 질문 배지/섹션 모두 없어야 함
    assert "가상 질문" not in resp.text


@pytest.mark.asyncio
async def test_document_graph_tab_empty(client, stores):
    """그래프가 없는 문서의 그래프 탭은 안내 메시지를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="No Graph",
        original_content="Text",
        content_hash="h3",
    )

    resp = await client.get(f"/partials/document/{doc_id}/graph")
    assert resp.status_code == 200
    assert "No graph data" in resp.text


@pytest.mark.asyncio
async def test_document_metadata_tab(client, stores):
    """메타데이터 탭이 문서 정보를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="upload",
        title="Meta Test",
        original_content="Content",
        content_hash="h4",
    )

    resp = await client.get(f"/partials/document/{doc_id}/metadata")
    assert resp.status_code == 200
    assert "upload" in resp.text
    assert "Meta Test" in resp.text


@pytest.mark.asyncio
async def test_document_status_api(client, stores):
    """문서 상태 API가 정상 동작한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Status",
        original_content="C",
        content_hash="h5",
    )

    resp = await client.get(f"/api/documents/{doc_id}/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_delete_document(client, stores):
    """문서 삭제 API가 정상 동작한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="To Delete",
        original_content="Del",
        content_hash="h6",
    )

    resp = await client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 204

    doc = await meta_store.get_document(doc_id)
    assert doc is None


@pytest.mark.asyncio
async def test_document_list_with_filter(client, stores):
    """소스 타입 필터가 동작한다."""
    meta_store = stores[0]
    await meta_store.create_document(
        source_type="manual", title="M1", original_content="A", content_hash="f1",
    )
    await meta_store.create_document(
        source_type="upload", title="U1", original_content="B", content_hash="f2",
    )

    resp = await client.get("/partials/document-list?source_type=manual")
    assert resp.status_code == 200
    assert "M1" in resp.text
    assert "U1" not in resp.text


@pytest.mark.asyncio
async def test_update_document(client, stores):
    """문서 수정 API가 정상 동작한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Original",
        original_content="Old content",
        content_hash="old",
    )

    resp = await client.put(
        f"/api/documents/{doc_id}",
        data={"title": "Updated", "content": "New content"},
    )
    assert resp.status_code == 204

    doc = await meta_store.get_document(doc_id)
    assert doc is not None
    assert doc["title"] == "Updated"
