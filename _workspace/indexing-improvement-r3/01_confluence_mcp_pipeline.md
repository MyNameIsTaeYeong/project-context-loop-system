# Confluence MCP 호출 이후 인덱싱 처리 흐름 (코드 추적)

진입 트리거 2가지:
- 수동 임포트: `POST /confluence-mcp/import-pages` (`web/api/confluence_mcp.py:248`)
- 자동/수동 싱크: `POST /confluence-mcp/sync-targets/{id}/trigger` (`web/api/confluence_mcp.py:453`) — 페이지/서브트리/스페이스 단위

## 전체 흐름

```
[사용자 UI / 스케줄러]
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Phase 1 — 임포트 (sync/mcp_sync.execute_sync_target)          │
│   - MCP 세션 열기                                            │
│   - 스코프별 페이지 enumerate (subtree/space/page)            │
│   - 각 페이지에 대해 import_page_via_mcp                     │
│       → mcp_confluence.get_page(session, page_id)            │
│       → _extract_page_content / _extract_page_title          │
│       → html_converter.convert_html_to_markdown(html_body)   │
│       → meta_store.create_document  또는                     │
│         meta_store.update_document_content (content_hash 비교)│
│           · raw_content (HTML 원본) 도 함께 저장              │
│   - SyncResult { created, updated, unchanged } 집계          │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ Phase 2 — 인덱싱 (sync/mcp_sync._run_processing_phase)        │
│   - 대상: created + updated + 기존 status='failed'           │
│   - asyncio.Semaphore(phase2_concurrency=5) 로 병렬 실행     │
│   - 각 doc_id 에 대해 process_document(...)                  │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ process_document (processor/pipeline.py:71)                  │
│                                                              │
│  1. start_reprocessing(meta_store, doc_id)                   │
│      └ 기존 파생 데이터 모두 삭제                            │
│        · vector_store.delete_by_document                     │
│        · graph_store.delete_by_document                      │
│        · meta_store.delete_chunks_by_document                │
│      └ status='processing'                                   │
│      └ processing_history 시작 row 생성                      │
│                                                              │
│  2. source_type 분기 — confluence_mcp 의 경우:               │
│                                                              │
│     2-1. 구조화 추출 (raw_content HTML → ExtractedDocument)  │
│         extract_confluence(raw_html)                         │
│           ├ sections[]    (헤딩 트리 + md_content)           │
│           ├ outbound_links[] (page/user/url/attachment/jira) │
│           ├ code_blocks[], tables[], mentions[]              │
│           └ plain_text   (마크다운 결과)                     │
│                                                              │
│     2-2. ★ 청킹 (chunker.chunk_extracted_document)           │
│         chunk_size=512, chunk_overlap=50                     │
│           - sections 트리 그대로 사용 (재파싱 없음)            │
│           - 원자 블록 보호: 펜스 코드(```) / 마크다운 테이블  │
│             은 chunk_size 초과해도 단독 청크로 유지          │
│           - 그 외는 토큰 기반 슬라이딩 윈도우 (overlap 50)   │
│           - 각 Chunk: {id(uuid), content, token_count,       │
│             section_path "A > B > C", section_anchor,        │
│             section_index}                                   │
│                                                              │
│     2-3. ★ 멀티뷰 임베딩                                     │
│         body_texts = [c.content for c in chunks]             │
│         meta_texts = [build_meta_view_text(title, section_path)│
│                       for c in chunks]                       │
│           (meta = "title\nsection_path", path 키워드 리콜용) │
│         embeddings = await embedding_client.aembed_documents │
│                       (body_texts + meta_texts)              │
│           - EndpointEmbeddingClient: POST /v1/embeddings     │
│           - 배치 100, 임베딩 모델 컨텍스트 8K (현재 가정)    │
│           - 청크 크기 < 8K 이라 안전                          │
│                                                              │
│     2-4. 벡터 DB 저장                                         │
│         vector_store.delete_by_document(doc_id)              │
│         vector_store.add_chunks(                             │
│             ids=[chunk_id+'#body', chunk_id+'#meta', ...],   │
│             embeddings=body+meta,                            │
│             documents=chunk.content × 2,                     │
│             metadatas=[{document_id, chunk_index, title,     │
│                         section_path, section_anchor,        │
│                         logical_chunk_id, view: 'body'/'meta'}│
│         )                                                    │
│         (ChromaDB collection "context_loop_chunks")          │
│                                                              │
│     2-5. SQLite chunks 저장                                  │
│         meta_store.delete_chunks_by_document(doc_id)         │
│         for chunk in chunks:                                 │
│             meta_store.create_chunk(                         │
│                 chunk_id, document_id, chunk_index, content, │
│                 token_count, section_path, section_anchor,   │
│                 section_index                                │
│             )                                                │
│                                                              │
│     2-6. 그래프 추출 (병렬 채널 3개, 같은 document 노드로 수렴)│
│                                                              │
│         a) 링크 그래프 (결정론, LLM 없음)                    │
│            build_link_graph(extracted, doc_title)            │
│              outbound_links → 그래프 엣지                    │
│              kind=page → external_doc 노드 + mentions edge   │
│              kind=user → person 노드                         │
│              kind=jira → ticket 노드                          │
│              kind=url  → external_url 노드                   │
│                                                              │
│         b) 본문 그래프 (결정론, LLM 없음)                    │
│            build_extraction_units(extracted, doc_id, title)  │
│              → ExtractionUnit[] (1500 토큰 응축)             │
│            extract_body_graph(units, doc_title)              │
│              API 엔드포인트 (POST /v1/x), Jira 키, 굵게 강조,│
│              표 헤더 등 시그널을 unit 단위로 추출             │
│              Relation.label = unit.section_path              │
│                                                              │
│         c) ★ LLM 본문 그래프 (R2 — 문서 단위 1회 호출)      │
│            extract_llm_body_graph_for_document(              │
│                doc_title=title,                              │
│                body=_assemble_document_body(extracted),      │
│                llm_client=llm_client                         │
│            )                                                 │
│              어휘 고정 + JSON 출력 강제                       │
│              ↳ depends_on/implements/calls/owned_by/uses…   │
│              입력 토큰 > 200K 면 InputTooLargeError →        │
│              extract_llm_body_graph(units, …) 폴백           │
│                                                              │
│         이상 3채널 모두 GraphStore.save_graph_data           │
│         → graph_nodes/graph_edges (SQLite + 옵션 NetworkX)   │
│         → 정규 노드 병합으로 같은 document 노드에 수렴       │
│                                                              │
│  3. storage_method 파생                                      │
│     chunks 있음 + graph 있음 → 'hybrid'                      │
│     chunks 없음 + graph 있음 → 'graph'                       │
│     그 외 (chunks만)         → 'chunk'                       │
│                                                              │
│  4. complete_reprocessing                                    │
│     - status='completed'                                     │
│     - processing_history row 마감 (status='success')         │
│     - new_storage_method 갱신                                │
│                                                              │
│  실패 시: complete_reprocessing(status='failed', error=...)   │
└──────────────────────────────────────────────────────────────┘
```

## 청크가 어디서 어떻게 쓰이는가 (사용처 정리)

| 경로 | 청크 사용 방식 |
|------|---------------|
| 검색 시 — `context_assembler._search_chunks` | ChromaDB 에서 query 임베딩과 유사한 청크 N개를 인출. `logical_chunk_id` 로 body/meta 뷰 dedup. distance 기반 정렬 |
| 검색 출처 — `assemble_context_with_sources` | 청크 metadata 의 `section_path` 를 "(섹션: …)" 라벨로 노출 (Confluence 페이지 내 deep-link 가능) |
| 답변 컨텍스트 조립 — MCP `mcp.context_max_tokens=4096`, `max_context_chunks=10` | 청크 10개를 4096 토큰 안에 결합하여 LLM 답변용 컨텍스트로 사용 |
| 리랭킹 — `reranker.rerank_chunks` | 청크 N개를 query 와 쌍으로 평가하여 top-k 재선별 |
| 대시보드 — `/partials/document/{id}/chunks` + `tab_chunks.html` | 청크 N개를 body/meta 멀티뷰로 표시 |
| 결정론 그래프 — `extract_body_graph` | `ExtractionUnit` (별개 분할, 청크와 무관) 사용 |
| LLM 그래프 — R2 함수 | `extracted.plain_text` / sections 합본 (청크 우회) |

## "청킹" 제거가 손대는 지점

옵션 D (작은 문서=1벡터 / 큰 문서=섹션 폴백) 기준 변경 지점:

| 모듈 | 변경 |
|------|------|
| `processor/chunker.py` | 신규 `chunk_extracted_document_doclevel(extracted, cfg)` — 토큰 한도 검사 후 1청크 또는 섹션 단위로 분할 |
| `processor/pipeline.py` | `chunk_extracted_document` 호출을 신규 함수로 교체. `cfg` 에 `max_embedding_tokens=8000` 같은 옵션 추가 |
| `mcp.context_max_tokens` (config) | 4096 → 32K 정도로 상향 (혹은 max_context_chunks 축소) |
| `web/api/documents.py` + `tab_chunks.html` | 청크 1건일 때 "Chunk #0" 대신 "Document Body" 같은 명명 (선택적, UX 개선) |
| `_format_chunk_results` / 출처 라벨 | section_path 빈 문자열 처리 — 분할된 경우에만 라벨 표시 (이미 코드가 `if section_path:` 가드 있음 — 무변경) |

`vector_store` / `meta_store.create_chunk` 스키마는 그대로 — chunk_index=0, section_path="" 인 단일 row 로 자연 작동.

## 임베딩 한도 8K — 어떻게 분할 결정하나

```
def chunk_extracted_document_doclevel(extracted, *, max_tokens=8000, ...):
    full_body = _assemble_full_body(extracted)   # sections 트리 합본
    total = count_tokens(full_body)

    if total <= max_tokens:
        # 1 벡터 — 문서 전체를 단일 청크로
        return [Chunk(content=full_body, token_count=total,
                      section_path="", section_anchor="", section_index=None)]

    # 큰 문서 — 섹션 단위 폴백 (R2 ExtractionUnit 입자도)
    return _chunk_by_section(extracted, max_tokens=max_tokens, ...)
```

`_chunk_by_section` 은 ExtractionUnit 빌더와 유사한 응축/분할 로직이지만 출력이 `Chunk` (section_path 보존).

## 답변 — Confluence MCP 호출 이후의 흐름

위 다이어그램 ★ 표시 3 곳이 청킹 관련 단계입니다:
- **2-2 청킹** (변경 대상)
- **2-3 멀티뷰 임베딩** (변경 영향)
- **2-6c LLM 그래프** (R2 에서 이미 문서 단위로 전환됨)
