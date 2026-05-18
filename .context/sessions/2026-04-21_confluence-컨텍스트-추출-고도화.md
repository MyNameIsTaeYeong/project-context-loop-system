# Confluence 컨텍스트 추출 고도화 + LLM 의존 제거

- 일시: 2026-04-21
- 브랜치: `claude/confluence-context-extraction-z6o9S`
- 관련 결정: D-039, D-040, D-041
- 커밋: 24e376c, 6c92dad, aae3ca7, 20eb4ed, e03848c, 6c70d20, 76e9082, 359ce55, 1e9a127, 10b23d6

## 배경

Confluence 문서는 Storage Format HTML로 들어오면서 섹션 계층, 페이지 간 링크, 코드블록, 테이블을 이미 기계 판독 가능한 태그로 실어 나른다. 그럼에도 기존 파이프라인은:

1. `html_to_markdown()`로 HTML 전체를 평탄화 → 구조 정보 소실
2. `graph_extractor.extract_graph()`(LLM)로 엣지 재추출 → 이미 명시된 정보를 LLM이 재해석하며 환각/토큰 비용 발생
3. 청커는 마크다운을 다시 헤딩 정규식으로 파싱 → 추출기가 계산한 path/anchor 무시, 코드블록이 경계에서 잘림

Git 코드는 D-036에서 AST 기반 정적 추출로 전환되었고, Confluence도 같은 "구조화된 입력에는 결정론적 파서를" 원칙을 적용할 시점.

## 작업 흐름

### Step 1 — 구조화 추출기 (D-039)

- 신규: `src/context_loop/ingestion/confluence_extractor.py`
  - 한 번의 BeautifulSoup 파싱으로 `ExtractedDocument(plain_text, sections, outbound_links, code_blocks, tables, mentions)` 반환
  - `Section`: 레벨/제목/앵커(slugify)/path/md_content
  - `OutLink`: `kind in {page, user, jira, attachment, url}`, target_id/title/space, in_section
  - `CodeBlock`: language + content + in_section (`ac:structured-macro[name=code/noformat]` + 표준 `<pre><code class="language-*">`)
  - `Table`: headers + rows + in_section
  - `Mention`: user/jira 참조
- `documents` 테이블 스키마: `raw_content` 컬럼 추가
- REST/MCP 수집 경로 모두 원본 HTML을 `raw_content`에 저장
- `processor/pipeline.py`의 Confluence 분기에서 `extract()` 호출 + 반환 dict에 `extraction` 메트릭(sections/outbound_links/code_blocks/tables/mentions 개수) 노출
- 테스트: 28건 (HTML 엣지 케이스, 섹션 path 계산, 링크 종류별 추출, 코드 매크로 언어 태그, 테이블 헤더/행 분리, 중첩 헤딩, Jira macro, ri:user vs ri:page, 앵커 slug 규칙)

### Step 2 — 결정론적 링크 그래프 (D-039)

- 신규: `src/context_loop/processor/link_graph_builder.py`
  - 순수 함수 `build_link_graph(extracted, *, doc_title) -> GraphData`
  - 매핑:
    ```
    page       → document / references
    user       → person   / mentions_user
    jira       → ticket   / mentions_ticket
    attachment → attachment / has_attachment
    url        → SKIP
    ```
  - **url 제외 이유**: (1) 병합 키가 `(name.lower(), type)`인데 URL은 쿼리/프래그먼트/트레일링 슬래시 변종이 너무 많아 병합 불가, (2) 내부 지식망 탐색에서 확장할 대상이 없어 순수 리프 노드, (3) `<a>` 태그 추출은 `ac:link`보다 신호 대 잡음비가 낮음, (4) 메타로는 `extracted.outbound_links`에 그대로 남아 있으므로 필요 시 LLM 컨텍스트/UI로 별도 활용 가능
  - self-entity 패턴: `Entity(doc_title, "document")`를 함께 생성 → GraphStore의 `(name_lower, type)` 병합으로 다른 문서에서 들어오는 references가 동일 노드로 수렴
  - 중복 제거: 동일 타겟 엔티티 1회만 생성, `(source, target, relation_type)` 3-튜플로 관계 dedup
- 파이프라인 Confluence 분기에서 `outbound_links`가 있을 때만 `save_graph_data()` 호출, 없으면 스킵
- 테스트: 13건 (kind별 매핑, url 스킵, self-entity 존재, 동일 타겟 중복, 동일 관계 중복, 다른 섹션 동일 링크, 빈 doc_title, 빈 outbound_links)

### LLM classifier/graph_extractor 전면 제거 (D-040)

AST(D-036) + 링크 그래프(D-039)로 모든 소스가 결정론적으로 처리되면서 LLM 기반 분류/추출 경로가 호출되지 않는 상태가 되었음.

- **삭제**
  - `src/context_loop/processor/classifier.py` 전체
  - `graph_extractor.py`에서 `_SYSTEM_PROMPT`, `_USER_PROMPT_TEMPLATE`, `_CODE_SYSTEM_PROMPT`, `_select_prompts`, `extract_graph`, `_extract_single`, `_extract_map_reduce`, `_split_content`, `_merge_graphs`, `_parse_graph_response`, 관련 상수 — `Entity`/`Relation`/`GraphData` dataclass만 남김 (graph_store, ast_code_extractor, link_graph_builder 공유)
  - `tests/test_processor/test_classifier.py`, `tests/test_processor/test_graph_extractor.py` 파일 전체
- **파이프라인 시그니처 변경**
  - `process_document()`에서 `llm_client`, `storage_method_override` 파라미터 제거
  - `storage_method`는 실제 저장 산출물에서 파생: chunks only → `"chunk"`, graph only → `"graph"`, 둘 다 → `"hybrid"`, 아무것도 없으면 `"chunk"` (기본)
- **호출 경로 정리**
  - `ingestion/coordinator.py`: `LLMClient` 임포트/파라미터/속성 제거, `_pipeline_available`에서 llm_client 체크 제외
  - `web/api/git_sync.py`: `get_llm_client` Depends / `pipeline_llm_client` 전달 제거
  - `web/api/documents.py`: `trigger_processing` 엔드포인트의 `llm_client` 의존성, `_run_pipeline` 시그니처에서 `llm_client` 파라미터, `process_document()` 호출의 `llm_client=` 키워드 모두 제거 (이 파일은 초기 리팩터에서 누락되어 사용자가 `documents.py:334 파라미터 에러` 리포트 → 1e9a127로 추가 수정)
- **유지**: chat/rerank/HyDE/query_expander/graph_search_planner 등 **검색 시점** LLM 경로. `get_llm_client` 자체와 `EndpointLLMClient`는 `web/api/chat.py` 및 외부 agent 스크립트가 계속 사용

### Step 3 — 구조화 추출 → 청커 직결 + 원자 블록 보호 (D-041)

- `Chunk` dataclass에 `section_anchor: str = ""` 필드 추가 (기본값으로 하위 호환)
- `chunker.chunk_extracted_document(extracted, ...)` 신설
  - `extracted.sections`를 그대로 순회 (마크다운 헤딩 정규식 재파싱 제거)
  - 각 섹션에 대해 `heading_line + "\n\n" + md_content` 형태로 조립 후 원자 블록 분리기에 위임
  - 청크에 `section_path` + `section_anchor` 기록
  - `extracted.sections`가 비어 있으면 `plain_text` 기반 `chunk_text()` 폴백 (하위 호환)
- `_split_markdown_blocks(text)` 헬퍼:
  - 펜스 코드블록(```...```)은 시작~종료 펜스 통째로 `_Block(atomic=True)`
  - 마크다운 테이블: 헤더 행 + 구분자 행(`|---|`) + 연속 파이프 행을 `_Block(atomic=True)`
  - 그 외는 빈 줄 기준 단락 분리 `_Block(atomic=False)`
- `_chunk_blocks(blocks, ...)`:
  - 일반 블록: `chunk_size` 초과 시 기존처럼 강제 분할
  - atomic 블록: 초과해도 **자르지 않고** 단독 청크로 방출(oversized 허용)
  - 블록 누적 → `chunk_size` 초과 시 flush(overlap)
- `chunk_text()`도 새 블록 분리기를 공유하도록 리팩터 — 마크다운 테이블/코드블록 원자성이 기존 소스(upload/manual)에도 적용됨
- 파이프라인 Confluence 분기가 `chunk_extracted_document` 호출
- VectorStore metadata에 `section_anchor` 필드 전파 (Confluence + git_code 양쪽)

## 테스트

| 대상 | 건수 | 비고 |
|------|-----:|------|
| confluence_extractor | 28 | 신규 |
| link_graph_builder | 13 | 신규 |
| chunker | +4 | 코드블록/테이블 원자성, anchor 전파, 빈 sections 폴백 |
| pipeline | 8 | 전면 재작성 (LLM mock 제거) |
| coordinator | - | `pipeline_llm_client`/`storage_method_override` 어설션 제거 |
| test_classifier | 삭제 | - |
| test_graph_extractor | 삭제 | - |

- `test_processor/` + `test_ingestion/`: 353건 통과
- `test_mcp/` + `test_storage/`: 87건 통과
- 회귀 없음

## 사용자 리포트 대응

- "documents.py 334라인 파라미터에러가 발생합니다." — 초기 coordinator/git_sync 리팩터에서 누락된 파일. `trigger_processing` 엔드포인트와 `_run_pipeline`에서 `llm_client`/`get_llm_client`/`LLMClient` 일체 제거 (커밋 1e9a127).
- "link_graph_builder의 url 제외 이유" — 병합 키 불안정 + 내부 지식망 탐색 가치 없음 + `<a>` 태그 신호 품질 + 메타로는 `extracted.outbound_links`에 보존됨. D-039 결정 섹션에 반영.

## 남은 한계 / 다음 작업 후보

- `section_anchor`의 UI/검색 노출 — 청크 탭의 섹션 딥링크, 검색 결과의 "이 섹션 열기" 버튼
- `extracted.code_blocks` / `extracted.tables`를 독립 검색 가능한 구조화 메타로 노출 (현재는 청크 본문 내부에만 존재, 별도 테이블/메타로 쿼리 가능하도록)
- Phase 9.9 (증분 처리 — git diff 기반 변경 파일만 재처리) 또는 9.10 (GitHub webhook 자동 동기화)
- 외부 URL 그래프 노드화(옵션) — 동일 외부 문서를 여러 위키가 참조하는 경우 추적. URL 정규화 규칙 + `entity_type="external_url"` 별도 설계 필요.
