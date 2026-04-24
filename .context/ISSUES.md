# 이슈 및 TODO 트래커

구현 중 발견된 이슈, 미결정 사항, 개선 아이디어를 기록한다.

## 미해결

### I-004: LLM Classifier 프롬프트 설계
- 문서를 chunk/graph/hybrid로 판정하는 프롬프트의 정확도와 비용 최적화 필요
- Phase 3.1 시작 시 프로토타이핑 및 테스트 필요

### I-005: Confluence HTML→MD 변환 품질
- 현재 정규식 기반 경량 변환 사용 — 복잡한 Confluence 매크로(표, 패널, 코드 블록 확장 등) 미지원
- Phase 3 또는 4 시작 전에 markdownify 라이브러리 도입 여부 결정 필요

### I-006: ConfluenceClient HTTP 연결 풀링
- 현재 매 요청마다 httpx.AsyncClient를 생성함 — 대량 임포트 시 성능 저하 가능
- Phase 4 이상에서 AsyncClient를 재사용하도록 리팩터링 고려

### I-007: save_document title 업데이트 미지원
- `ingestion/editor.py`의 `save_document()`가 기존 문서 수정 시 title을 업데이트하지 않음
- 현재 `web/api/documents.py`에서 직접 SQL UPDATE로 우회 중
- editor.py 자체를 수정하여 title 업데이트를 지원하는 것이 바람직

### I-008: 채팅 인터페이스 마크다운 렌더링 제한
- 현재 chat.js에서 간단한 정규식 기반 마크다운 → HTML 변환 사용 (bold, code, newline만 지원)
- marked.js 등 전문 마크다운 라이브러리 도입 시 코드 블록, 목록, 테이블 등 완전 렌더링 가능
- Phase 7 또는 고도화 시점에 검토

### I-009: 채팅 대화 이력 미저장
- 현재 채팅 대화는 브라우저 세션에서만 유지되며 서버에 저장하지 않음
- 대화 이력 DB 저장 및 이전 대화 재개 기능은 향후 고도화 항목

### I-010: Confluence MCP Client 연동 구현
- Confluence REST API 접근이 차단되어 사내 Confluence MCP Server를 통한 문서 임포트 방식 채택 (D-016)
- `ingestion/mcp_confluence.py` 신규 모듈 구현 필요
- 사내 MCP 서버 전송 방식(SSE/stdio) 및 각 도구의 입출력 형식 확인 필요
- 3가지 임포트 시나리오(검색, 트리 탐색, 내 문서) 구현
- 웹 UI (탭 기반) 및 API 엔드포인트 추가
- **진행 상태 (2026-04-23)**: 3-scope 싱크로 재설계되어 D-043 으로 백엔드 완성. 검색 기반 진입 + page/subtree/space 3범위 등록·싱크·해제의 REST API 와 동시성·안전 속성까지 완료. 수동 "검색 결과에서 페이지 선택 → 임포트" 와 "MCP `search_content` 기반 프리뷰" 는 기존 엔드포인트(`POST /api/confluence-mcp/search`, `POST /api/confluence-mcp/import`)가 유지된다.
- **진행 상태 (2026-04-24)**: I-030 해결로 3-scope 싱크 UI 까지 완성. 남은 항목은 트리 탐색형 UI 와 내 문서 임포트 UI 정도.

### I-011: Confluence REST API 접근 차단
- 사내 보안 정책으로 Confluence REST API 직접 호출 불가
- 기존 `ingestion/confluence.py` (ConfluenceClient) 사용 불가능
- MCP Client 방식(I-010)으로 대체 예정

### I-019: Git Repository 기반 코드 수집기 구현
- Git repo를 clone/pull하여 소스 코드 파일을 수집하고, 변경 감지(commit hash 비교)로 증분 동기화하는 `ingestion/git_repository.py` 모듈 필요
- GitPython 또는 subprocess 기반 구현 검토 필요
- 파일 필터링 규칙 (`.gitignore` 존중, 바이너리 제외, 언어별 확장자 필터) 설계 필요
- D-025 관련, Phase 9.2

### I-020: 코드 → LLM 문서 자동 생성 파이프라인
- ~~수집된 코드 파일을 LLM에 전달하여 자연어 문서(code_doc)를 자동 생성하는 파이프라인 필요~~
- **방향 전환 (D-036)**: LLM 기반 문서 생성 대신 AST 기반 정적 추출로 전환. 코드에서 심볼(함수/클래스/메서드)과 import 관계를 직접 추출하여 벡터DB + GraphStore에 저장. LLM 호출 불필요.
- D-025 관련, Phase 9.3

### I-021: document_sources 테이블 및 검색 시 원본 코드 첨부
- `document_sources` 테이블 추가하여 code_doc ↔ git_code N:M 관계 관리
- context_assembler에서 code_doc chunk 반환 시 원본 코드를 함께 제공하는 옵션 구현
- D-026, D-030 관련, Phase 9.7에서 git_code 저장 + document_sources 연결을 함께 구현

### I-022: LLM 생성 문서의 환각 검증 메커니즘
- ~~code_doc이 원본 코드와 불일치하는 경우를 감지하는 방법 검토 필요~~
- **해결 (D-036)**: AST 기반 정적 추출로 전환하여 환각 문제 자체가 제거됨. LLM이 코드를 해석/요약하지 않고, 파서가 구문 구조를 그대로 추출하므로 정보 왜곡 불가.

### I-023: 멀티에이전트 Coordinator/Product/Worker/Category 구현
- D-027 아키텍처에 따른 4종 에이전트 구현 필요
- `asyncio.gather` 기반 병렬 실행, 부분 실패 허용 (graceful degradation)
- Worker 단위: 디렉토리 기반 + 크기 제한 (30개 초과 시 분할, 3개 미만 시 병합)
- Phase 9.4, 9.5, 9.6
- **진행 상태**: Coordinator(9.4) + Worker Agent(9.5) 구현 완료. Category Agent(9.6) 잔여.

### I-024: 카테고리 프롬프트 시스템 설계
- D-028에 따라 카테고리를 config 프롬프트로만 정의하여 코드 변경 없이 확장 가능하게
- 기본 카테고리 5종 프롬프트 작성 필요: architecture, development, infrastructure, pricing, business
- 팀별 추가 카테고리(보안, QA 등) 대응 구조 검증 필요
- Phase 9.3, 9.6

### I-025: 에이전트별 엔드포인트/모델 설정 구조
- D-029에 따라 Worker(Haiku급), Synthesizer(Sonnet급), Category(Opus급) 각각 다른 엔드포인트 지정 가능해야 함
- 기존 `llm_client.py`의 `EndpointLLMClient`를 에이전트별로 인스턴스화
- 미지정 시 기존 `llm.endpoint` 폴백
- Phase 9.3

### I-026: 모노레포 상품 스코프 자동 제안 기능
- ~~LLM이 레포 디렉토리 트리를 분석하여 상품별 스코프를 제안하는 기능~~
- 방향 전환: config에 상품명만 정의하면 레포를 스캔하여 관련 파일 경로를 자동 탐지하는 방식으로 변경
- **진행 상태**: `resolve_product_paths()` 구현 완료. LLM 기반 자동 제안(2-pass, 레이어 감지) 코드 제거.

### I-027: scope_analyzer 상품 식별 정확도 개선
- 방향 전환: LLM/레이어 감지 기반 → config 기반 상품명 + 파일명 토큰 매칭 방식으로 교체
- **해결 완료**:
  - `resolve_product_paths()`: config 상품명 기반으로 레포 전체에서 파일 경로 자동 탐지
  - `_filename_matches_product()`: 경계 인식 토큰 매칭으로 오탐 방지
  - `_plural_variants()`: 복수형 변형 지원 (vpcs, policies, addresses 등)
  - `parse_product_scopes()`: paths 미정의 시 자동 탐지 연동 (기존 수동 paths와 하위 호환)

### I-028: 오버로드 메서드 FQN 충돌
- D-038의 파일 범위 FQN(`file::Class.method`)은 동일 이름·다른 시그니처 오버로드를 구분하지 못함 — Java/Kotlin 등에서 `foo(int)` / `foo(String)`이 단일 엔티티로 dedup됨
- 해결 방향 후보: FQN에 시그니처 해시 접미사 추가, 또는 엔티티 properties에 overload index 저장
- 현재는 상대적으로 드문 케이스라 우선순위 낮음

### I-029: 그래프 스키마 프롬프트 FQN 노출 최적화
- D-038 이후 LLM에게 제공되는 그래프 스키마 요약에 FQN(`file.py::Class.method`)이 그대로 노출되어 토큰 소모가 늘고, LLM이 FQN 그대로 응답하는 경향
- `get_neighbors`의 짧은 이름 fallback이 동작을 보장하지만, 스키마 요약에서 짧은 이름만 노출하거나 FQN 표기 가이드를 프롬프트에 추가하면 품질/토큰 모두 개선 여지
- 우선순위: 중간. 현재는 fallback으로 문제 없음.

## 해결됨

### I-035: Phase 2 인덱싱 자동화 + 동시성 + 실패 재시도 → 해결 (2026-04-24, D-044)
- **증상**: 싱크 후 문서는 meta 에 저장되지만 벡터/그래프 인덱싱은 수동 "Process" 버튼 필요. Phase 2 가 직렬이라 대량 싱크 시 느림. 인덱싱 실패 문서는 `status='failed'` 로 stuck.
- **해결 3단계** (`2178564` + `c5c9ca6`):
  1. `execute_sync_target` 에 선택적 `embedding_client`/`pipeline_config` 주입 → Phase 2(process_document) 자동 실행. `SyncResult` 에 `processed`/`processing_errors` 버킷 + UI `◎N indexed · ⚠M indexing-failed` 노출
  2. `asyncio.Semaphore(5)` + `asyncio.gather` 로 Phase 2 병렬화. `config.processor.phase2_concurrency` 운영자 튜닝 가능. 400 문서 기준 벽시계 ~5배 단축, OpenAI 기본 tier rate limit 의 ~5% 사용
  3. `MetadataStore.list_failed_member_doc_ids(target_id)` 신설 — target 스코프 내 status='failed' 문서를 JOIN 쿼리로 식별. Phase 2 큐 = `created + updated + failed-in-membership`(중복 제거) → 재싱크마다 자동 재시도
- 테스트 +11 (Phase 2 기본·unchanged skip·실패 격리·skip 조건·summary 확장·failed_member JOIN·concurrency bound·failed 재시도·clamp)

### I-034: CQL 페이지네이션 서버 cap 대응 → 해결 (2026-04-24, `49add3c`)
- **증상**: CQL `searchContent` 가 totalSize=356 을 돌려주는데 실제 임포트는 그보다 적음
- **근본 원인 2가지 협력**:
  1. `size < page_size` 에서 무조건 break — 일부 서버가 요청 `limit` 과 무관하게 응답당 개수를 cap (예: `limit=100` 요청에 `size=25` + `totalSize=500`) 하면 첫 응답 직후 종료되어 대량 누락
  2. `start += page_size` (요청값 증분) — 서버 cap 으로 25개만 왔는데 start 를 100 증가 → items 25–99 구간 스킵
- **해결**: `_paginate_cql` 공통 헬퍼 추출 + `total_size` 가 알려진 경우 short-page 휴리스틱 skip + `start += env.size` 로 실제 반환 개수만큼 전진. 관측성 보강: `_sync_subtree` 가 `estimate_subtree_page_count` 선행 호출해 totalSize vs 실제 열거 수 비교, 불일치 시 warning 로그
- 테스트 +2 (`_make_capping_search_session` fake 로 server cap 재현 — space/subtree 각 1건)

### I-033: 서브트리 BFS 누락 → CQL ancestor 평탄 열거로 전환 → 해결 (2026-04-24, `d507b15`, D-044)
- **증상**: "하위 포함" 등록 시 최하위 depth 까지 가져오지 않음. walker 기반 BFS 가 몇 가지 경로로 누락 가능:
  1. per-parent `getChild` 의 독립 페이지네이션 오판
  2. 중간 노드 예외 격리가 그 아래 서브트리 전부 손실
  3. `max_depth=20` 초과
  4. `type` 필드가 `"page"` 아닌 값/누락으로 드롭
  5. 권한 차이 per-node 호출
- **해결**: 사용자 제안대로 CQL `ancestor = X AND type = "page"` 로 서버 측 평탄 열거. `_subtree_cql`/`estimate_subtree_page_count`/`enumerate_subtree_pages` 신설, `_sync_subtree` 가 descendants + 루트 수동 prepend (CQL ancestor 는 루트 자신 미포함)
- **Trade-off**: membership 의 `parent_page_id`/`depth` 가 NULL 로 저장됨. 현재 코드베이스에서 이 컬럼을 읽는 곳이 없어 실영향 없음. hierarchy 가 필요해지면 별도 hydrate 단계 추가
- `walk_subtree` 자체는 삭제하지 않고 유지 (다른 러너 호환)
- 테스트 +9 (CQL helpers 3 + sync 재작성 6)

### I-032: 서브트리 임포트 시 하위 페이지 누락 — structuredContent 누락 + envelope 키 변종 → 해결 (2026-04-24, `3bb7685`)
- **증상**: 하위 포함 클릭 시 루트만 임포트되고 자식 전부 누락
- **근본 원인 2가지 동시에**:
  1. MCP 신규 스펙은 JSON 을 `CallToolResult.structuredContent` 에 직접 담기도 하는데, 기존 `_parse_json_result` 는 `content[].text` 만 읽어서 전체 페이로드가 소실됨. getChild 결과가 빈 리스트로 해석되어 walker 가 루트만 반환
  2. 서버 구현체마다 envelope 키가 `results` / `children` / `page` / `pages` / `items` 또는 `{page: {results:[...]}}` 중첩 형태로 다양
- **해결**: `_parse_json_result` 가 `structuredContent` 를 먼저 확인. `_unwrap_envelope(parsed) -> (items, envelope)` 공통 헬퍼 신설로 5가지 키 변종 + 1단계 중첩 흡수. `expand` 기본값 `""` → `"page"` (빈 문자열 거부하는 서버 방어)
- 테스트 +4 (structuredContent 우선, children 키, 중첩 page.results, expand non-empty)

### I-031: MCP tool 필수 파라미터 검증 에러 → 해결 (2026-04-24, `de0d7fb` + `9536c0c`)
- **증상**: UI 에서 공간 검색 클릭 시 `CallToolResult validation` 에러. 사내 MCP 서버가 `getSpaceInfoAll` 에 `start`/`limit` 필수로 요구. 같은 날 `getChild` 도 `pageId`/`start`/`limit`/`expand` 필수로 확인
- **해결**:
  - `get_all_spaces` 가 `{"start": 0, "limit": 100}` 전달 + envelope 응답 페이지네이션 (`de0d7fb`)
  - `get_child_pages` 가 `pageId`/`start`/`limit`/`expand` 함께 전달 + 동일 페이지네이션 로직 (`9536c0c`)
  - 둘 다 envelope 없이 list 만 오는 서버 변종도 한 번에 처리 후 종료
- 테스트 +5

### I-030: Confluence MCP 3-scope 싱크 UI 구현 → 해결 (2026-04-24)
- `web/templates/confluence_mcp.html` 단일 파일 확장으로 완료. `syncTargetsPanel()` Alpine 컴포넌트 신설.
- 구현된 요소:
  - 🔍 검색 박스: `GET /api/confluence-mcp/search?q=...` → Spaces / Pages 두 섹션으로 분리 렌더, 빈 쿼리 시 모든 공간 노출
  - 결과 카드 3버튼: 📄 페이지만 / 🌿 하위 포함 / 🏢 공간 전체 (Space 카드는 🏢 단일 버튼)
  - 확인 다이얼로그 3종: subtree(즉시), space(estimate 선행 후 "예상 N개" 표시), unregister(cascade 경고)
  - 등록된 대상 카드 목록: scope 뱃지 색상(`scope-page`/`subtree`/`space`) + 상대시간 last_sync + 증분 요약 monospace 뱃지(+N new · ~N updated · -N removed) + [🔄 재싱크] [❌ 해제]
  - 폴링: running/queued 가 하나라도 있을 때만 2초 간격 `GET /sync-targets/{id}` 자가 시작·정지
- 구 단발성 임포트 UI(3탭)는 `<details>` 로 접어 "고급" 영역에 보존
- 회귀 테스트: `tests/test_web/test_confluence_mcp_sync_api.py` 19건 모두 통과 (UI 자체는 E2E 없음, 백엔드 계약을 따름)

### I-003: 엔티티 병합 테이블 스키마 미정 → 해결 (Phase 7.7, D-024)
- `graph_node_documents` 조인 테이블로 노드-문서 다대다 관계 관리
- `entity_name(대소문자 무시) + entity_type` 기준 정규 노드 병합

### I-012: HTML→Markdown 변환 시 테이블·매크로 손실 → 해결 (Phase 7.1, D-018)
- BeautifulSoup + markdownify 기반 변환으로 교체
- Confluence 매크로 전처리 지원 (info/warning/note/code/expand 등)

### I-013: 청킹 시 문서 구조(헤딩) 미활용 → 해결 (Phase 7.2, D-019)
- 마크다운 헤딩 기반 계층적 청킹 + `section_path` 메타데이터 첨부

### I-014: 그래프 추출 시 콘텐츠 절삭 → 해결 (Phase 7.4, D-021)
- map-reduce 방식으로 전체 문서 처리, 엔티티/관계 중복 제거 병합

### I-015: 컨텍스트 조립 시 재랭킹·필터링 부재 → 해결 (Phase 7.3, D-020)
- cosine similarity threshold + LLM 리랭커 2단계 필터링

### I-016: 쿼리 전처리 및 확장 부재 → 해결 (Phase 7.5, D-022)
- HyDE 적용 — LLM 가상 문서 임베딩과 원본 쿼리 임베딩 평균

### I-017: 문서 분류기가 처음 2000자만 사용 → 해결 (Phase 7.6, D-023)
- 시작/중간/끝 구간 샘플링 (~4000자)

### I-018: 크로스-문서 엔티티 병합 로직 미구현 → 해결 (Phase 7.7, D-024)
- 정규 노드 방식 — entity_name + entity_type 기준 병합, graph_node_documents 조인 테이블

### I-001: 웹 프레임워크 최종 선택 → FastAPI + Jinja2 + HTMX
- 2026-03-11 결정: FastAPI + Jinja2 + HTMX + Alpine.js + Pico CSS
- 커스터마이징 자유도 높고 MCP 서버와 같은 프로세스 실행 가능

### I-002: 그래프 시각화 라이브러리 → vis.js
- 2026-03-11 결정: vis.js (CDN)
- 네트워크 그래프에 특화, 구현 공수 적음, 기본 인터랙션(줌/드래그/클릭) 제공
