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
- 수집된 코드 파일을 LLM에 전달하여 자연어 문서(code_doc)를 자동 생성하는 파이프라인 필요
- 관련 파일 그룹핑 전략 (디렉토리 기반? 모듈 기반? LLM 판단?) 결정 필요
- LLM 프롬프트 설계: 코드의 목적, 구조, 설계 의도, 의존성을 포함하는 문서 생성
- 생성된 문서는 기존 chunker + graph_extractor로 처리 (기존 파이프라인 재사용)
- D-025 관련, Phase 9.3

### I-021: document_sources 테이블 및 검색 시 원본 코드 첨부
- `document_sources` 테이블 추가하여 code_doc ↔ git_code N:M 관계 관리
- context_assembler에서 code_doc chunk 반환 시 원본 코드를 함께 제공하는 옵션 구현
- D-026 관련, Phase 9.1, 9.5

### I-022: LLM 생성 문서의 환각 검증 메커니즘
- code_doc이 원본 코드와 불일치하는 경우를 감지하는 방법 검토 필요
- 생성 시점 검증 (LLM에게 코드와 문서 비교 요청) vs 검색 시점 검증 (원본 코드 첨부) 결정 필요
- D-025, D-026 관련

## 해결됨

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
