# 구현 진행 상황

## 현재 단계
- **Phase**: Phase 7.17 — `/api/chat` 성능 최적화 (LLM 호출 병렬화 + 전용 cross-encoder 리랭커 도입)
- **Step**: 사용자 보고 "/api/chat 응답이 느리다" → RAG 파이프라인 LLM 호출 4건(HyDE / 그래프 plan / 리랭킹 / 답변)의 직렬 실행 진단. 데이터 의존성 분석 결과 리랭킹 ↔ 그래프 계획이 무관함을 확인 → `asyncio.gather` 병렬화. 후속으로 LLM 기반 리랭커(D-021)를 dedicated cross-encoder 모델 호출로 교체 — D-045.
- **최신(2026-04-29)**: 브랜치 `claude/optimize-chat-api-performance-vODmW`. 6개 커밋 누적.
  1. **rerank ↔ graph plan 병렬화**: `_rerank_and_search_graph(...)` 헬퍼로 두 LLM 호출을 `asyncio.gather` 동시 실행. `assemble_context` (MCP) 와 `assemble_context_with_sources` (/api/chat) 양쪽에서 동일 헬퍼 사용 → 두 호출 경로 동기화 누락 위험 제거.
  2. **전용 리랭커 클라이언트 도입**: `processor/reranker_client.py` 신설(`RerankerClient` ABC + `EndpointRerankerClient`). 요청 본문 `{model, query, documents}`, 응답은 4가지 형식(Cohere/Jina dict, TEI list-of-dict, ordered scores dict/list) 자동 정규화. `processor/reranker.py` 130 → 80 라인 단순화 (LLM 시스템 프롬프트, JSON 추출, 정규식 fallback 모두 제거). 점수 의미 변경 0~10 → 0~1, `reranker_score_threshold` 기본값 4.0 → 0.3.
  3. **config 신규 섹션 `reranker:`**: `endpoint`, `model`, `api_key`, `headers` (기존 `llm:` 와 동일 스타일). `_build_reranker_client` 가 `endpoint`/`model` 미설정 시 None 반환 → 리랭킹 자동 스킵 (graceful).
  4. **`include_source_code` 호출자 누락 수정 + 기본값 True**: 어셈블러에 매개변수가 있었지만 `/api/chat`, MCP `search_context` 양쪽에서 전달이 빠져 항상 False 로 동작하던 버그 수정. `ChatRequest.include_source_code: bool = True` + `search_context(..., include_source_code=True)` 추가. code_doc/code_summary 의 원본 git 소스가 기본 첨부됨.
  5. **`/api/chat` `max_tokens` 2048 → 8192**: 답변 잘림 방지.
- 테스트 누적 +18 (rerank 9 신규 + reranker_client 9 신규), processor + mcp 262 passed. 회귀 없음. `tests/test_web/test_api_chat.py` 사전 실패 4건은 baseline 동일(Jinja2/Starlette 픽스처).
- 미해결 후속:
  1. HyDE 단계 임베딩-LLM 병렬화 (HyDE 사용 시에만 효과, 우선순위 낮음).
  2. `/api/chat` 스트리밍 응답 (TTFB 단축, SSE 엔드포인트 별도 작업).

## 이전 단계
- **Phase 7.12~7.16(2026-04-28)**: 그래프 컨텍스트 고도화 — Extraction Unit + 본문 의미 관계 + 어휘 단일 출처. 브랜치 `claude/improve-graph-context-veDSQ`. 8개 PR 누적.
  1. **PR-1 ExtractionUnit 빌더**: 추출 전용 입자도(target=1500/max=2400 토큰). 후위 순회 merged_tokens → top-down 응축/분할/흡수 4규칙. 안정적 `section_id = f"{document_id}:{section_index}"` + breadcrumb 주입(`# 문서:`, `## 위치:`, 머리말). 기존 chunker 와 분리.
  2. **PR-2 chunk ↔ unit 조인 키**: `chunks.section_index INTEGER` 컬럼 + idempotent 마이그레이션 + pipeline 전달. ExtractionUnit 의 section_ids 와 정수 키로 조인 가능. 향후 검색 시 그래프 결과 → 본문 스니펫 인입의 인프라.
  3. **PR-3 결정론 본문 추출기 + Option A**: `body_extractor.py` 신설 4가지 시그널(API/Jira/bold/table). 회고 후 보수적 기본값으로 변경 — API/Jira 만 ON, bold/table OFF (작성 컨벤션 의존 + 추상 헤더 노이즈). 의미 관계는 LLM 추출기에 위임.
  4. **PR-4 LLM 본문 추출기**: `llm_body_extractor.py` 신설. 9개 의미 관계(depends_on/implements/calls/owned_by/supersedes/has_part/uses/provides/documented_in) + 7개 엔티티(system/module/api/concept/policy/person/team) 어휘 고정. 어휘 외 / 끝점 누락 / self-loop 검증 단계 드롭. 단위 격리 + 비용 게이트(min_unit_tokens=200, skip_split_overlap_parts). **opt-in**: `PipelineConfig.enable_llm_body_extraction=True` + `llm_client` 둘 다 전달 시에만.
  5. **perf**: `_collect_units` 13447ms → 1.8ms (~7500배). cProfile 로 진단해보니 `tiktoken/load.py:read_file` 가 vocabulary 를 매 호출마다 재다운로드 시도(357 sections × 38ms). `chunker._get_tokenizer` 에 `lru_cache(maxsize=8)` 적용 + `_Node.own_body` 캐시 + `_render_subtree/_dfs` 단일 walk 통합.
  6. **호출 체인 통합**: `process_document(llm_client=...)` 옵셔널 파라미터 추가, 4개 사이트 전파(coordinator Git, mcp_sync Confluence, web/api/documents 수동 재처리, web/api/git_sync). DI: `app.state.llm_client` → `Depends(get_llm_client)`. 테스트 conftest 에 `app.state.llm_client = None` 주입.
  7. **fix 빈 응답**: `process_document` 흐름에서 LLM 본문 추출 응답이 비는 증상. Qwen3 등 reasoning 모델이 `<think>...</think>` 사고에 max_tokens 모두 소진 + `extract_json` 정규식 스트립 후 빈 문자열. `graph_search_planner` 와 동일 처방 적용 — `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` + max_tokens 1024 → 2048.
  8. **PR-5 graph_vocabulary + planner 시스템 프롬프트 갱신**: `graph_vocabulary.py` 단일 출처(12 entity + 19 relation + 7 intent→relation 매핑). 추출기 어휘 ⊆ vocabulary 강제 테스트로 미래 누락 자동 검출. `_render_system_prompt()` 동적 렌더 → LLM 플래너가 어휘 의미 + 의도 매핑 가이드를 알게 됨. "Auth Service 의존?" → `focus_relations=["depends_on", "uses", "calls"]` 정확 선택 가능.
- 테스트 누적 +90건(extraction_unit 21 + body 25 + llm_body 21 + vocab 7 + planner +2 + chunker/metadata/pipeline 회귀 보강), 311 통과 + 환경 의존 15 baseline 동일.
- **남은 갭**:
  - 처음 진단 6가지 원인 중 (3) 엔티티 병합 키 불안정 — 부분적, (4) 메타 축 부재 — 미진행, (5) 검색 결과 렌더링 빈약 — 미진행
  - 추출은 강해졌지만 사용자에게 가치가 전달되는지(검색 품질) 측정 부재 → **다음 세션 1순위**
  - cross-document 관계 출처 보존 (NetworkX DiGraph 단일 엣지 한계로 last-write-wins) → 향후 Option A
- **이전(2026-04-24)**: 브랜치 `claude/plan-document-sync-ui-aOBNT`. 하루 세션에 2단계로 전개.
  1. **오전 — UI 배포 (I-030 해결)**: `web/templates/confluence_mcp.html` 단일 파일 확장. `syncTargetsPanel()` Alpine 컴포넌트 + 3 버튼 카드(📄/🌿/🏢) + 3 확인 다이얼로그(subtree 안내·space estimate·unregister cascade 경고) + 등록 대상 카드 목록(scope 뱃지·상대시간·증분 요약) + 자가 시작·정지 폴링(2초 간격, running/queued 없으면 자동 정지). 구 단발성 임포트 UI 는 `<details>` 로 보존.
  2. **오후 — 트러블슈팅 체인 5개 이슈 수정 (D-044)**:
     - I-031 MCP 필수 파라미터: `getSpaceInfoAll` 에 `start`/`limit`, `getChild` 에 `start`/`limit`/`expand` 필수. 페이지네이션 포함해 수정
     - I-032 서브트리 루트만 임포트: `CallToolResult.structuredContent` 채널 누락 + envelope 키 변종(results/children/pages/page/items + 1단 중첩) 대응. `_parse_json_result` 가 structuredContent 우선, `_unwrap_envelope` 공통 헬퍼 신설, `expand` 기본값 `""` → `"page"`
     - I-033 BFS 누락 → CQL 전환: walker 가 5가지 경로로 누락 가능(per-parent 페이지네이션·중간 노드 예외·max_depth·type 필드·권한). 사용자 제안대로 `ancestor = X AND type = "page"` 평탄 열거로 전환. `_subtree_cql` / `estimate_subtree_page_count` / `enumerate_subtree_pages` 신설, `_sync_subtree` 가 CQL descendants + 루트 수동 prepend. `walk_subtree` 는 유지 (다른 러너 호환)
     - I-034 페이지네이션 서버 cap 버그 2개: (a) `size < page_size` 에서 무조건 break — totalSize 알려지면 skip 하도록, (b) `start += page_size` → `start += env.size` 로 실제 반환 개수만큼 전진. `_paginate_cql` 공통 헬퍼로 중복 제거하며 동시 수정
     - I-035 Phase 2 인덱싱 자동화 + 동시성 + 실패 재시도: `execute_sync_target` 에 선택적 `embedding_client`/`pipeline_config`/`phase2_concurrency` 주입 → created+updated+failed-in-membership 자동 인덱싱. `asyncio.Semaphore(5)` + `asyncio.gather` 로 400 문서 기준 벽시계 ~5배 단축. `MetadataStore.list_failed_member_doc_ids(target_id)` JOIN 쿼리로 재싱크마다 stuck failed 문서 자동 재시도
- **이전(2026-04-23)**: Confluence MCP 3-scope(page/subtree/space) 싱크 백엔드 9단계 완성 (D-043). 브랜치 `claude/confluence-continuous-fetch-aOGCB`. 사내 Confluence REST 차단(I-011) 환경에서 "특정 페이지만 / 특정 페이지 하위 전체 / 공간 전체" 3가지 범위를 동일한 디스패처(`execute_sync_target`)로 처리. 참조 카운팅 membership 모델(`confluence_sync_membership`)로 공유 페이지(subtree+space 동시 등록) 안전성을 확보 — 한쪽 해제 시 다른 소유자가 있으면 문서 보존, 양쪽 모두 해제 시 `delete_document_cascade` 로 완전 삭제. 두 핵심 안전 속성을 테스트로 고정: (1) walker/enumerate 실패 시 membership 보존(일시 장애가 cascade 삭제로 번지지 않음), (2) 개별 import 실패가 stale 삭제로 번지지 않음(walker 가 확인한 페이지는 import 성공 여부 무관하게 current_ids 포함). 웹 API 7종 + target_id 별 asyncio.Lock + BackgroundTasks 기반 비동기 실행. 테스트 +110건 전부 통과. UI (검색박스/3버튼 카드/확인 다이얼로그/등록카드+폴링 진행률)는 I-030 으로 분리.
- **이전(2026-04-22)**: 멀티뷰 임베딩 Phase 1 도입 (D-042). 일반 문서 분기에서 청크당 ChromaDB 엔트리 2개 저장 — `{id}#body`(임베딩=본문) + `{id}#meta`(임베딩=title+section_path), 두 엔트리 document 동일, `logical_chunk_id`로 dedup. `context_assembler._search_chunks`에 over-fetch(×2) + dedup 로직 추가. 기존 body 뷰는 그대로 유지되므로 본문 친화 질의는 손해 없고, 경로/제목 친화 질의는 meta 뷰로 리콜 상승. SQLite `chunks`는 논리 청크 1행 유지. title/section_path 모두 비면 meta 뷰 생략. 테스트 +3 (pipeline 2, context_assembler 1) — 전체 450건 통과(web 제외).
- **이전 상태**: 3단계로 완료. (1) **Step 1** — `ingestion/confluence_extractor.py` 신설 (ExtractedDocument: sections/outbound_links/code_blocks/tables/mentions), `documents.raw_content` 컬럼 추가 및 REST/MCP 수집 경로에서 원본 HTML 보존, 파이프라인 Confluence 분기가 추출기를 호출하도록 주입 (D-039). (2) **Step 2** — `processor/link_graph_builder.py` 신설: `OutLink` → `GraphData` 결정론적 변환 (`page/user/jira/attachment` → 각각 entity_type + relation, `url`은 병합 키 불안정 이슈로 제외). self-entity 패턴으로 GraphStore의 `(name, type)` 병합을 통한 인접 문서 간 엣지 수렴. 파이프라인 Confluence 분기에 통합 (D-039). (3) **LLM classifier/graph_extractor 전면 제거** — 결정론적 대체(AST + 링크 그래프)가 모든 소스를 커버하므로 `classifier.py` 삭제, `graph_extractor.py`에서 Entity/Relation/GraphData 스키마만 남기고 LLM 프롬프트·extract_graph·맵리듀스 제거. `process_document()`에서 `llm_client`/`storage_method_override` 파라미터 제거, `storage_method`는 실제 저장 산출물에서 파생(chunks only=`chunk`, graph only=`graph`, 둘 다=`hybrid`). coordinator/documents/git_sync 호출 경로 정리 (D-040). (4) **Step 3** — `Chunk.section_anchor` 필드 추가, `chunk_extracted_document()` 신설로 `extracted.sections`를 그대로 소비(헤딩 재파싱 제거). `_split_markdown_blocks()`이 펜스 코드블록(```)과 마크다운 테이블을 `atomic=True`로 묶어 청크 경계 중간 분할 금지, 일반 블록은 기존처럼 강제 분할. `section_anchor`를 VectorStore metadata까지 전파 (D-041). 테스트: confluence_extractor 28건, link_graph_builder 13건, chunker +4건(코드블록/테이블 원자성 + anchor 전파 + 빈 sections 폴백), pipeline 8건 전면 재작성, coordinator/documents/git_sync 호출 정합성 확보. 총 회귀 없음 (440+건). 다음: (a) section_anchor를 UI 청크 탭/검색 결과 deep-link에 노출, (b) `extracted.code_blocks`/`tables`를 별도 구조화 메타로 검색에 활용, (c) Phase 9.9(증분 처리) 또는 9.10(GitHub webhook).

## Phase별 진행률

### Phase 1: 기반 구조
- [x] 1.1 프로젝트 스캐폴딩 (pyproject.toml, 디렉토리 구조)
- [x] 1.2 설정 관리 (config.yaml 로드/저장, 기본값)
- [x] 1.3 인증 모듈 (keyring 연동, 토큰 저장/조회)
- [x] 1.4 SQLite 메타데이터 저장소 세팅

### Phase 2: 문서 입력 파이프라인
- [x] 2.1 파일 업로드 처리 (MD/TXT/HTML → 원본 저장)
- [x] 2.2 마크다운 직접 작성 저장
- [x] 2.3 Confluence API 임포트 (인증, 스페이스/페이지 조회, HTML→MD 변환)
- [x] 2.4 Confluence 증분 동기화
- [x] 2.5 문서 변경 감지 및 재처리 파이프라인 (Delete & Recreate)
- [x] 2.6 Confluence MCP Client — 3-scope 싱크 end-to-end (백엔드 D-043 + UI I-030 + 자동 인덱싱·동시성·실패 재시도 D-044, I-031~I-035)
- [ ] 2.7 Confluence MCP Client — 탐색형 UI (서브트리 트리 뷰, 검색 기반 진입은 완료)
- [ ] 2.8 Confluence MCP Client — 내 문서 임포트 (`getUserContributedPages` 백엔드는 존재, UI 미구현)

### Phase 3: LLM 저장 방식 판단 + 처리
- [x] 3.1 LLM Classifier 구현 (문서 분석 → chunk/graph/hybrid 판정)
- [x] 3.2 텍스트 청킹 모듈 (토큰 기반 분할)
- [x] 3.3 임베딩 + ChromaDB 벡터 저장
- [x] 3.4 그래프 엔티티/관계 추출 모듈
- [x] 3.5 그래프DB 저장 (NetworkX + SQLite)
- [x] 3.6 그래프 엔티티 병합 및 고아 엣지 정리 로직

### Phase 4: 웹 대시보드
- [x] 4.1 기본 대시보드 레이아웃 (문서 목록, 통계)
- [x] 4.2 문서 상세 뷰 (원본 탭, 청크 탭, 메타데이터 탭)
- [x] 4.3 그래프 시각화 탭 (인터랙티브 그래프 렌더링)
- [x] 4.4 Confluence 임포트 UI (연결 설정, 스페이스 브라우저)
- [x] 4.5 마크다운 에디터 통합
- [x] 4.6 파일 업로드 UI

### Phase 5: MCP Server
- [x] 5.1 MCP 서버 기본 구조 (FastMCP, stdio 전송)
- [x] 5.2 `search_context` Tool 구현 (벡터 검색 + 그래프 탐색 → 컨텍스트 조립)
- [x] 5.3 `list_documents`, `get_document`, `get_graph_context` Tool 구현
- [x] 5.4 SSE 전송 지원 (선택적 원격 접근)
- [x] 5.5 MCP 클라이언트 연동 테스트 (Claude Code 등)

### Phase 6: 질의 및 고도화
- [x] 6.1 대시보드 내 채팅 인터페이스 (RAG 파이프라인 활용)
- [x] 6.2 출처 표시 (원본 문서 링크)

### Phase 7: 답변 품질 개선 (RAG 파이프라인 고도화)
- [x] 7.1 HTML→Markdown 변환기 개선 — 테이블, 매크로, 중첩 목록 지원 (I-012)
- [x] 7.2 헤딩 기반 계층적 청킹 + 섹션 메타데이터 첨부 (I-013)
- [x] 7.3 Cross-encoder Reranker 추가 + 유사도 threshold 도입 (I-015)
- [x] ~~7.4 그래프 추출 시 전체 문서 처리 — map-reduce 방식 (I-014)~~ — LLM graph_extractor 전면 제거 (D-040)
- [x] 7.5 쿼리 확장(Query Expansion) — HyDE 적용 (I-016)
- [x] ~~7.6 문서 분류기 입력 범위 확대 (I-017)~~ — classifier 전면 제거 (D-040)
- [x] 7.7 크로스-문서 엔티티 병합 (I-018, I-003)
- [x] 7.8 Confluence Storage Format 구조화 추출기 (D-039)
- [x] 7.9 Confluence outbound_links → 결정론적 링크 그래프 (D-039)
- [x] 7.10 LLM classifier/graph_extractor 제거 — 결정론적 파이프라인 확정 (D-040)
- [x] 7.11 구조화 추출 → 청커 직결 + 코드블록/테이블 원자 보호 (D-041)
- [x] 7.12 ExtractionUnit 빌더 — 추출 전용 듀얼 그래뉼러리티(target=1500/max=2400 토큰) + chunk ↔ unit 조인 키(`section_index`) (PR-1, PR-2 / 2026-04-28)
- [x] 7.13 결정론 본문 추출기 — API/Jira ON, bold/table OFF (Option A 보수적 디폴트) (PR-3 / 2026-04-28)
- [x] 7.14 LLM 의미 관계 추출기 — 9개 관계 + 7개 엔티티 어휘 고정, opt-in (PR-4 / 2026-04-28)
- [x] 7.15 graph_vocabulary 단일 출처 + 플래너 시스템 프롬프트 어휘 가이드 (PR-5 / 2026-04-28)
- [ ] 7.16 검색 품질 측정 도구 — 골드 셋 + 자동 채점, ON/OFF 비교, 추출 결과 샘플 덤프 (I-040, **다음 세션 1순위**)
- [ ] 7.17 그래프 검색 결과 렌더링 개선 — 본문 스니펫 인입(section_index 활용), 경로 서술, 출처 링크 (I-041)
- [ ] 7.18 메타 축 노드화 — author/space/label 노드 승격 (I-042)

### Phase 8: 배포
- [ ] 8.1 패키징 및 사내 배포
- [ ] 8.2 초기 설정 마법사 (대시보드 내)

### Phase 9: 추가 컨텍스트 소스 — Git 코드 기반 멀티에이전트 문서 생성
- [x] 9.1 `document_sources` 테이블 추가 (code_doc ↔ git_code 연결, D-026)
- [x] 9.2 `ingestion/git_repository.py` — Git repo clone/pull, 상품별 스코핑, 변경 감지
- [x] 9.3 config에 `sources.git` 섹션 추가 — 상품 정의, 카테고리 프롬프트, 에이전트별 엔드포인트 (D-028, D-029)
- [x] 9.4 Coordinator Agent 구현 — 전체 파이프라인 조율 (D-027)
- [x] ~~9.5 Worker Agent 구현~~ — Worker Agent 제거 (D-034)
- [x] ~~9.6 Category Agent 구현~~ — Level 2/3 제거 (D-033)
- [x] 9.7 원본 코드 저장 (git_code) + document_sources 연결 (D-025, D-026, D-030)
- [x] 9.8 git_code → 기존 파이프라인 직접 연결 (hybrid 고정, Classifier 건너뜀) (D-034)
- [ ] 9.9 증분 처리 — git diff 기반 변경 파일만 재처리
- [ ] 9.10 GitHub webhook 기반 자동 동기화
- [ ] 9.11 커밋 히스토리 / PR 리뷰 수집 및 컨텍스트화

### Phase 10 (후속): 추가 소스 확장
- [ ] 10.1 Jira API 연동 — 티켓, 요구사항-코드 연결
- [ ] 10.2 DB 스키마 수집 — DDL 스냅샷, 도메인 모델 그래프화
- [ ] 10.3 API 명세 (OpenAPI/Swagger) 자동 파싱

## 마지막 업데이트
- 일시: 2026-04-23
- 내용: Confluence MCP 3-scope 싱크 백엔드 9단계 완성 (D-043, 세션 로그 `2026-04-23_confluence-mcp-3scope-sync-backend.md`).
  - **배경**: 사내 Confluence REST 차단(I-011) 환경에서 "특정 공간 일부 문서를 지속적으로 가져오기" 를 요구. 범위는 3가지(페이지 단건 / 서브트리 / 공간 전체) + 검색 진입 + 버튼 트리거로 합의. 기존 `SyncEngine`(REST) 은 MCP 세션 수명·도구 셋이 달라 확장 불가. 동시에 "해제 시 임포트된 문서도 삭제" + "공간 전체 + 서브트리 동시 등록 허용" 두 정책 충돌(공유 페이지 오삭제)을 참조 카운팅 membership 으로 해결.
  - **구현된 9단계 (각 단계 단일 커밋 + 테스트)**:
    1. `storage/cascade.py::delete_document_cascade(doc_id, *, meta_store, vector_store, graph_store) -> bool` — web/api/documents.py 에 인라인되어 있던 vector→graph→meta 삭제 순서를 facade 로 추출. 해제 경로와 문서 API 가 공유. (commit 2410a80)
    2. `ingestion/mcp_confluence::SearchEnvelope` + `search_content_envelope` — `searchContent` envelope 의 `totalSize`/`size`/`start`/`limit` 보존. `total`/`totalSize`/`_totalSize` 변종 + list-only + 빈 응답 3경로 처리. (commit 3b908ac)
    3. `get_page_with_ancestors(session, page_id)` + 순수 함수 `format_breadcrumb(page)` — `space.name → ancestors[].title → page.title` 을 " / " 로 결합, key/name 폴백, 빈 항목 스킵, id 최후 폴백. (commit fc20735)
    4. `walk_subtree(session, root_id, *, max_depth=20, max_pages=5000)` — `getChild` BFS, 방문 집합(사이클 차단), type != "page" 필터, 가지별 실패 격리. 루트는 `parent_id=None, depth=0, title=""` 로 첫 항목. (commit d564eec)
    5. `estimate_space_page_count` + `enumerate_space_pages` — CQL `space="KEY" AND type="page"` 페이지네이션 async generator. 종료 3중(totalSize 도달 / 짧은 페이지 / 빈 응답) + max_pages 상한. `_space_cql` 이 `\`/`"` escape. (commit 30e0e6a)
    6. 스키마: `confluence_sync_targets(scope CHECK IN (page|subtree|space))` + `UNIQUE(scope, space_key, COALESCE(page_id, ''))` — SQLite 기본 UNIQUE 가 NULL 을 distinct 로 취급하는 문제를 COALESCE 표현식 인덱스로 우회. `confluence_sync_membership(target_id FK CASCADE, page_id, ...)` PK `(target_id, page_id)` — 같은 page 가 여러 target 에 공존 허용. (commit 899b5f6)
    7. MetadataStore CRUD 8종: targets `upsert/get/list(created_at DESC, id DESC 타이브레이크)/update_sync_result/delete_sync_target`, membership `upsert/upsert_batch/list_page_ids/remove_memberships`. 핵심은 `_find_orphans_if_membership_dropped` — 가상 삭제 시 "다른 target 이 소유하지 않는 page" 만 선정해 cascade 대상 doc_id 반환. 실제 cascade 는 호출측(delete_document_cascade) 책임. (commit 6d15192)
    8. `sync/mcp_sync.py::execute_sync_target` 디스패처 + scope 별 3핸들러. **두 안전 속성 명시적 구현**: (a) 열거(walker/enumerate) 실패 시 membership 수정 안 함 — 일시 장애로 cascade 삭제 유발 방지, (b) `current_ids.add()` 를 import try 블록 앞에 두어 개별 import 실패가 stale 삭제로 번지지 않음. (commit a7d94cc)
    9. 웹 API 7개: `GET /search`(spaces+pages 병합 + totalSize), `GET /spaces/{key}/estimate`, `POST/GET/DELETE /sync-targets`, `POST /sync-targets/{id}/sync`. target_id 별 `asyncio.Lock` + `_target_status` dict + BackgroundTasks 로 비동기 실행. `get_space_info` MCP 래퍼도 추가. (commit 2b233b0)
  - **테스트**: +110건 전부 통과 (cascade 4, 스키마 13 + CRUD 18, MCP 유틸 +42, 디스패처 14, 웹 API 19). 기존 실패 14건은 사전 Jinja2 환경 문제로 회귀 아님.
  - **아키텍처 성질**: `execute_sync_target` 은 session/stores 만 받으면 돌아가는 순수 함수 — BackgroundTasks / 주기 루프 / CLI / 테스트 어디서든 재사용. MetadataStore 는 diff 만 계산, cross-store cascade 는 facade 가 담당 — 두 계층 분리로 재시도·복구 로직 삽입 여지.
  - **남은 작업**
    - I-030: UI 구현 — 검색 박스 + 3버튼(📄 페이지만 / 🌿 하위 포함 / 🏢 공간 전체) 카드 + 확인 다이얼로그(하위 포함 / 공간 전체 / 해제) + 등록된 대상 카드 목록 + 폴링 진행률. `web/templates/confluence_mcp.html` 확장 + vanilla JS.
    - 자동 주기 실행 — 현재 버튼 트리거만. `MCPSyncEngine` + `auto_sync_enabled` 토글 기반 백그라운드 루프는 후순위. (기존 REST `SyncEngine` 과 별도 클래스, execute_sync_target 재사용)
    - SSE 진행률 — 현재 폴링. 수천 페이지 공간 싱크 UX 개선에 유용하나 필수 아님.
- 이전 (2026-04-21): Confluence 컨텍스트 추출 고도화 + LLM 기반 분류/추출 제거 (D-039, D-040, D-041).
  - **배경**: Confluence 문서는 Storage Format으로 섹션/링크/코드블록/테이블이 이미 기계 판독 가능한 구조로 들어온다. 그럼에도 파이프라인은 이 구조를 `html_to_markdown()`으로 평탄화해 버리고, 그래프 엣지는 LLM(`graph_extractor`)으로 다시 "추출"해 왔음 — 정보 손실 + 환각 + 토큰 비용의 3중 낭비.
  - **Step 1 — 구조화 추출기** (commits 24e376c, 6c92dad, aae3ca7; D-039)
    - `ingestion/confluence_extractor.py` 신설. BeautifulSoup 단일 파싱으로 `ExtractedDocument(plain_text, sections, outbound_links, code_blocks, tables, mentions)` 반환
    - `sections`: 헤딩 스택 기반 path + anchor(slugify) + md_content
    - `outbound_links`: `ac:link` → page/user/attachment, 일반 `<a>` → url, Jira macro → jira
    - `code_blocks`: `ac:structured-macro[name=code/noformat]` + 표준 `<pre><code class="language-*">`
    - `tables`: 헤더 + 행 구조화
    - `documents` 테이블에 `raw_content` 컬럼 추가, REST/MCP 수집 경로가 원본 HTML을 그대로 저장
    - 파이프라인 Confluence 분기에서 `extract()` 호출 + 반환 dict에 extraction 메트릭 노출
    - 테스트: confluence_extractor 28건
  - **Step 2 — 결정론적 링크 그래프** (commits 20eb4ed, e03848c; D-039)
    - `processor/link_graph_builder.py` 신설. `OutLink` → `GraphData` 순수 함수 변환
    - 매핑: `page → document/references`, `user → person/mentions_user`, `jira → ticket/mentions_ticket`, `attachment → attachment/has_attachment`
    - `url`은 제외 — 병합 키 불안정(쿼리/프래그먼트/슬래시 변종), 내부 지식망 탐색에서 확장 대상 없음, 외부 URL은 `extracted.outbound_links`에 메타로 잔존
    - self-entity(`Entity(doc_title, "document")`) 패턴으로 GraphStore의 `(name_lower, type)` 병합 활용 — 다른 문서에서 들어오는 references가 동일 노드로 수렴
    - 엔티티/관계 중복 제거: 동일 타겟 반복 시 1회만 생성, `(source, target, relation_type)` 3-튜플로 관계 dedup
    - 테스트: link_graph_builder 13건
  - **LLM classifier/graph_extractor 전면 제거** (commits 6c70d20, 76e9082, 359ce55, 1e9a127; D-040)
    - AST 기반 git_code(D-036) + 결정론적 Confluence 링크 그래프(D-039)로 모든 소스 커버 → LLM 의존 경로 코드만 남은 상태
    - `processor/classifier.py` 삭제, `graph_extractor.py`에서 LLM 프롬프트·`extract_graph`·맵리듀스·파서 제거, `Entity`/`Relation`/`GraphData` dataclass만 남김 (graph_store, ast_code_extractor, link_graph_builder 공유 스키마)
    - `process_document()`에서 `llm_client`/`storage_method_override` 파라미터 제거. `storage_method`는 실제 저장 산출물에서 파생 — chunks only=`chunk`, graph only=`graph`, 둘 다=`hybrid`
    - 호출 경로 정리: `CoordinatorAgent`(pipeline_llm_client 제거), `web/api/documents.py`(`_run_pipeline` 시그니처/의존성), `web/api/git_sync.py` 4개 파일. chat/rerank/HyDE 등 **검색 시점** LLM 경로는 그대로 유지
    - 테스트: `test_classifier.py`/`test_graph_extractor.py` 삭제, `test_pipeline.py` 8건 재작성, `test_coordinator.py`의 `pipeline_llm_client`/`storage_method_override` 어설션 제거
  - **Step 3 — 구조화 추출 → 청커 직결 + 원자 블록 보호** (commit 10b23d6; D-041)
    - `Chunk.section_anchor: str = ""` 필드 추가 (기본값으로 하위 호환)
    - `chunk_extracted_document(extracted, ...)` 신설 — `extracted.sections`를 그대로 순회, 헤딩 재파싱 제거. `section_path` + `section_anchor`를 청크 메타에 기록
    - `_split_markdown_blocks()`: 펜스 코드블록(```)과 마크다운 테이블(헤더+`|---|` 구분자) 연속 파이프 행을 `_Block(atomic=True)`로 묶음. 일반 텍스트는 기존처럼 빈 줄 단락으로 분리
    - `_chunk_blocks()`: 일반 블록은 기존처럼 `chunk_size` 초과 시 강제 분할, atomic 블록은 **자르지 않고** 단독 청크로 방출(oversized 허용)
    - 파이프라인 Confluence 분기가 `chunk_extracted_document` 호출. `section_anchor`를 VectorStore metadata(Confluence + git_code 양쪽)에 전파 — 검색 결과에서 Confluence 섹션 deep-link 구성 가능
    - 테스트: chunker +4건(코드블록 원자성/테이블 원자성/section path·anchor 전파/빈 sections → plain_text 폴백), pipeline 테스트 4건 패치 대상 갱신
  - **검증**: `test_processor/` + `test_ingestion/` 353건, `test_mcp/` + `test_storage/` 87건 통과. 회귀 없음.
  - **남은 제한 / 다음 작업**
    - `section_anchor` UI 활용 — 청크 탭의 섹션 딥링크, 검색 결과 "이 섹션 열기" 버튼
    - `extracted.code_blocks`/`extracted.tables`를 독립 검색 가능한 구조화 메타로 노출 (현재는 청크 본문에만 포함)
    - Phase 9.9(증분 처리) 또는 9.10(GitHub webhook)
- 이전 (2026-04-17): AST 기반 코드 그래프의 엔티티 병합 충돌 / imports 엣지 유실 수정 (D-038).
  - **배경**: D-036/D-037 도입 후 git sync 실행에서 (a) 외래키 제약 실패로 일부 엣지 누락, (b) contains 엣지가 엉뚱한 파일의 동일 이름 심볼을 가리키는 현상 발생.
  - **버그 1 — imports 엣지 target 유실** (commit 87a2784)
    - `to_graph_data()`가 import 모듈을 엔티티로 등록하지 않아 `save_graph_data`의 `name_to_node_id`에 없음 → 엣지가 DEBUG 로그와 함께 조용히 버려짐
    - 수정: `extraction.imports` 순회하며 `entity_type="module"` 엔티티 선등록 (파일 title과 동일/중복 제외)
  - **버그 2 — 동일 이름 심볼의 canonical 병합 충돌** (commit a291c46)
    - `save_graph_data`는 `(entity_name_lower, entity_type)` 기준으로 병합. `__init__`, `run`, `create` 같은 흔한 이름이 파일/클래스 간에 단일 노드로 합쳐짐
    - 수정: 엔티티 이름을 `file.py::name` / `file.py::Class.method` 형태의 파일 범위 FQN으로 스코핑
    - `_symbol_fqn()`/`_class_fqn()` 헬퍼. 심볼 루프를 parent 루프보다 먼저 실행해 Go `struct` 같은 특수 타입이 덮어써지지 않도록 보장. `seen_class_fqns`로 parent 중복 생성 차단
  - **버그 3 — get_neighbors 짧은 이름 검색 회귀** (commit 305d810)
    - FQN 도입으로 LLM이 반환한 짧은 이름(`create_user`)으로 탐색 시 결과 없음
    - 수정: `get_neighbors`에 3단 fallback 매칭 — 정확 매칭 → 스코프 이름(`::` 이후) → 짧은 이름(마지막 `.` 세그먼트)
    - 정확 매칭 우선으로 기존 사용처 무회귀
  - **테스트**: ast_code_extractor 38개(신규 5), graph_store 34개(신규 4)
  - **남은 제한**: Java/Kotlin 오버로드 메서드 dedup, LLM 스키마 프롬프트 최적화
- 이전 (2026-04-16): git_code를 LLM 기반에서 AST 기반 정적 추출로 전환 (D-036, D-037).
  - **배경**: D-035의 LLM 기반 코드 그래프 추출에서 빈번한 타임아웃 발생. 코드는 구조화된 데이터이므로 LLM 추론이 불필요 — AST 정적 분석으로 100% 정확한 추출 가능.
  - **D-036**: `ast_code_extractor.py` 신규 모듈
    - Python: `ast` 모듈 기반 정확한 파싱
    - Go/Java/TS/JS: 키워드 + 중괄호 매칭
    - 추출: 함수/클래스/메서드/struct/interface 심볼 + import 관계
    - `pipeline.py`: `source_type == "git_code"` 시 AST 경로 분기, LLM 호출 완전 우회
    - `coordinator.py`: `storage_method_override` `"graph"` → `"hybrid"` — 벡터 검색 활성화
    - 임베딩/저장 분리: 임베딩(이름+시그니처+docstring) vs 저장(전체 코드)
  - **D-037**: 메서드 단위 청킹
    - 클래스 내부 메서드를 개별 청크로 분할, `parent_name`/`parent_signature`로 소속 추적
    - `section_path`: `file > class > method` 계층 구조
    - Go 리시버 메서드, Java/TS/JS 클래스 메서드 모두 지원
    - 클래스→메서드 `contains` 관계 자동 생성
    - 메서드 없는 클래스(데이터 클래스)는 기존처럼 단일 심볼 유지
  - **전체 흐름**: git clone → store git_code → extract_code_symbols() → to_chunks() + to_graph_data() → 벡터DB + GraphStore
  - **테스트**: ast_code_extractor 33개 + 기존 141개 통과
- 이전 (2026-04-15): 코드 전용 그래프 스키마 + graph-only 처리 (D-035).
  - **D-035**: git_code를 코드 전용 프롬프트로 graph-only 처리
  - **변경 사항**:
    - `graph_extractor.py`: `source_type` 파라미터 추가. `_CODE_SYSTEM_PROMPT` + `_select_prompts()` — git_code는 코드 전용 엔티티(function, class, struct, interface, package, module, endpoint, error_type, constant, type_alias) + 관계(calls, imports, implements, contains, returns, depends_on, raises, receives) 프롬프트 사용
    - `pipeline.py`: `doc["source_type"]`을 `extract_graph()`에 전달
    - `coordinator.py`: `storage_method_override`를 `"hybrid"` → `"graph"`로 변경 — 코드 chunking 건너뜀
  - **기존 문서**: 변경 없음 — `source_type`이 `git_code`가 아니면 기존 프롬프트 사용
  - **GraphStore/검색**: 변경 없음 — entity_type은 자유 문자열이므로 코드/문서 엔티티 자연 공존
  - **전체 흐름**: git clone → store git_code → pipeline(graph_extractor with 코드 전용 프롬프트, chunking 건너뜀)
  - **테스트**: 380개 비-web 테스트 전체 통과 (신규 8개: 코드 프롬프트 선택, 코드 엔티티/관계 파싱, map-reduce)
- 이전 (2026-04-13): Phase 9.7 원본 코드 저장 (git_code) + document_sources 연결 구현 완료.
  - **핵심 구현** (`ingestion/coordinator.py`)
    - `ProductResult`에 `files: list[FileInfo]`, `repo_url: str` 필드 추가
    - `run_and_store()`에서 `store_git_code()`로 원본 코드 → `git_code` 문서 DB 저장
    - `git_code_map` (relative_path → document_id) 구축 후 document_sources 연결:
      - code_summary ↔ git_code: file_summaries의 relative_path로 1:1 매칭
      - code_doc ↔ git_code: `_collect_git_code_ids()`로 source_directories 기반 매칭
    - `run()`은 side-effect-free 원칙 유지 (D-031)
  - **원본 코드 첨부** (`mcp/context_assembler.py`)
    - `include_source_code: bool = False` 파라미터 추가 (opt-in)
    - `_extract_doc_ids()`: 검색 결과에서 document_id 추출
    - `_fetch_and_format_source_code()`: document_sources를 따라 git_code 원본을 마크다운 코드 블록으로 포맷
    - 중복 git_code 제거 (seen_source_ids), 언어 힌트 자동 추출
  - **검증 스크립트** `scripts/run_git_code_store.py` (기존 `run_phase97_test.py`에서 리네임)
    - 기존 스크립트(`run_worker_agent.py`, `run_category_agent.py`)와 동일한 `--config`/`-c` + `--full-pipeline` 패턴 적용
    - 3가지 모드: 샘플 레포(기본), `--full-pipeline`(config yaml), `--repo`(로컬 레포)
    - Mock Agent 기반 E2E 검증 9섹션: run() 전달, git_code 저장, code_summary 연결, code_doc 연결, 역방향 조회, 멱등성, _collect_git_code_ids, 원본 코드 첨부, DB 통계
  - **테스트**: coordinator 24개 + context_assembler 22개 전체 통과 (기존 418개 비-web 테스트 무회귀)
  - **설계 결정**: D-031 — git_code 저장과 document_sources 연결을 run_and_store()에서 수행
- 이전 (2026-04-09): Phase 9.6 Category Agent — 서버 과부하 대응 및 안정화.
  - **Map 배치 병렬 제어**: 무제한 병렬 → `asyncio.Semaphore(4)` 최대 4개 동시 실행
    - 초기: `asyncio.gather`로 전체 병렬 → 서버 "peer closed connection" 오류
    - 1차 대응: 직렬 처리로 전환 → 안정적이나 느림
    - 최종: 세마포어(4)로 제한된 병렬 → 속도와 안정성 균형
    - `max_concurrent_batches` 파라미터로 조정 가능
  - **스트리밍 기능 제거**: `EndpointLLMClient`의 `stream` 파라미터 및 `_complete_stream()` 삭제
    - `git_config.build_llm_client()`의 `stream` 파라미터 삭제
    - `scripts/run_category_agent.py`의 `stream=True` 호출 제거
    - 스트리밍 전용 테스트 8개 → 일반 응답 테스트 3개로 교체
  - **배치 크기 축소**: `max_chars_per_batch` 15000 → 8000 (prefill 타임아웃 방지)
  - 테스트 25개(category_agent) + 3개(llm_client) 전체 통과
- 이전 (2026-04-08): Phase 9.6 Category Agent 구현 완료 + Map-Reduce 타임아웃 대응.
  - **`ingestion/category_agent.py`** 신규: `LLMCategoryAgent` 클래스
    - Level 2 디렉토리 문서를 종합하여 카테고리별 관점 문서(Level 3) 생성
    - config의 카테고리 프롬프트를 system 프롬프트로 사용 (D-028)
    - orchestrator 엔드포인트(Opus급 고성능 모델) 사용 (D-029)
    - **글자수 기반 동적 배치 + Map-Reduce**: `max_chars_per_batch=8000` 기준으로
      디렉토리 요약을 배치로 분할. 배치 1개이면 단일 호출, 2개 이상이면
      Map(배치별 부분 문서 최대 4개 병렬 생성) → Reduce(부분 문서 종합) 2단계 처리
    - Map: max_tokens=8192, Reduce/단일: max_tokens=16384
    - Map 배치 일부 실패 허용, 부분 문서 1개만 남으면 Reduce 생략
    - 빈 입력 시 빈 문서 반환 (LLM 호출 안 함)
  - **`processor/llm_client.py`**: `EndpointLLMClient`에 `timeout` 파라미터 추가
    - 기본 600초, connect 10초. 대형 입력 처리 시 안전망 역할
    - `git_config.build_llm_client(agent, timeout=...)` 으로 에이전트별 설정 가능
  - **수동 테스트 스크립트** `scripts/run_category_agent.py`
    - `--input-dir` 모드: Worker 출력(_level2_summary.md)을 읽어 Category Agent 실행
    - `--full-pipeline` 모드: Git clone → Worker → Category Agent 순차 실행
    - `--categories` 옵션: 특정 카테고리만 선택 실행
    - 결과를 `scripts/output/{product}/category/{category}.md`에 저장
  - **Coordinator 수정 불필요**: 기존 `_run_category_agent()` 메서드가
    `CategoryAgentProtocol`을 통해 `LLMCategoryAgent`를 그대로 호출
  - 다음: 원본 코드 저장(9.7) — git_code DB + document_sources 연결
- 이전: Phase 9.5 Worker Agent 구현 + Coordinator 리팩토링 완료.
  - **`ingestion/worker_agent.py`** 신규: `LLMWorkerAgent` 클래스
    - Level 1 (파일 요약): worker LLM, max_tokens=4096, `enable_thinking=False`
    - Level 2 (디렉토리 문서): synthesizer LLM, max_tokens=8192, `enable_thinking=False`
    - 관점 중립 사실 요약. 개별 파일 실패 허용. `asyncio.Semaphore(max_concurrent_files=5)` 병렬 처리.
    - 장문 절삭(`max_file_tokens` 초과 시)
    - 테스트 13개 전체 통과
  - **Coordinator `_process_repository` 리팩토링 (D-030)**
    - `sync_repository()` 제거 → `clone_or_pull()`만 호출 (불필요한 git_code DB I/O 제거)
    - scopes를 직접 순회하며 상품별 `collect_files([scope])` → `_process_product()` 호출
    - `parse_product_scopes(clone_dir=...)` 전달 버그 수정
  - **수동 테스트 스크립트** `scripts/run_worker_agent.py`
    - `--local-dir` 모드: 로컬 파일 직접 분석
    - `--full-pipeline` 모드: Git clone → 상품별 Worker 실행
    - 결과를 `scripts/output/{product}/{directory}/` 하위에 마크다운 파일로 저장
    - `_level1_{filename}.md` (파일별 요약) + `_level2_summary.md` (디렉토리 종합)
  - **설계 결정**: D-030 — git_code DB 저장을 Phase 9.7로 분리
  - 다음: Category Agent(9.6) 구현
- 이전: Phase 9.4+ — `scope_analyzer.py` config 기반 전면 전환 (I-026, I-027 해결).
  - **LLM 기반 → config 기반 전환**: 956줄 → ~120줄. 2-pass 아키텍처, 레이어 감지, LLM 프롬프트 전량 삭제
  - **새 아키텍처**: config에 상품명 정의 → `resolve_product_paths()`가 BFS로 레포 전체 순회 → 파일명 토큰 매칭
  - **핵심 기능**: `_plural_variants()` 복수형 생성, `_filename_matches_product()` 경계 인식 토큰 매칭
  - **exclude 패턴**: fnmatch glob으로 tests/, vendor/ 등 제외 가능
  - **`parse_product_scopes()` 연동**: paths 미지정 시 자동 탐지, 수동 paths 우선
  - **테스트 스크립트**: `scripts/run_product_paths.py` — config yaml 읽어 zero-argument 실행
  - 테스트 24+4개 전체 통과
- 이전: Phase 9.4 — `ingestion/coordinator.py` 구현 완료 (D-027).
  - `CoordinatorAgent`: 전체 파이프라인 조율 (config 검증 → git sync → 상품별 분류 → Worker/Category 디스패치)
  - `WorkerAgent`/`CategoryAgentProtocol`: Protocol 기반 인터페이스 (Phase 9.5/9.6에서 LLM 구현)
  - `asyncio.Semaphore`로 max_concurrent_workers 동시성 제어
  - `store_directory_summary()`: code_summary 저장 (Level 2, 멱등)
  - `store_category_document()`: code_doc 저장 + document_sources 연결 (Level 3)
  - `run_and_store()`: 파이프라인 실행 + DB 저장 통합 메서드
  - 테스트 14개 (Mock Worker/Category Agent로 E2E 검증) — 전체 통과
- 이전: Phase 9.3 — `ingestion/git_config.py` 구현 완료 (D-028, D-029).
  - `GitSourceConfig` 타입 안전 dataclass (sources.git 전체 설정 파싱)
  - `CategoryConfig`: 카테고리 정의 + source_id 생성 (상품×카테고리 매트릭스)
  - `ProcessingConfig` + `LLMEndpointConfig`: 에이전트별 엔드포인트 설정
  - `resolve_endpoint()`: 에이전트별 → 글로벌 llm.* 폴백 해소 (D-029)
  - `build_llm_client()`: EndpointLLMClient 팩토리
  - `validate()`: 필수 설정 검증 (레포/카테고리/엔드포인트)
  - `load_git_source_config()`: Config 인스턴스에서 GitSourceConfig 로드
  - 테스트 26개 — 전체 통과
- 이전: Phase 9.2 — `ingestion/git_repository.py` 구현 완료.
  - Git repo clone/pull (asyncio subprocess)
  - 상품별 스코핑 (config paths/exclude glob 패턴)
  - git diff 기반 증분 변경 감지
  - git_code 문서 저장 (content_hash 기반 생성/갱신/무변경 판별)
  - 삭제 파일 처리, 디렉토리별 그룹핑 (Worker Agent 배정 단위)
  - 테스트 31개 (단위 16 + 통합 15) — 전체 통과
