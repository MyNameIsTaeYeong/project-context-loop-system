# 구현 진행 상황

## 현재 단계
- **Phase**: Phase 9 — 추가 컨텍스트 소스 (Git 코드 기반 컨텍스트 구축)
- **Step**: 9.0 설계 검토 완료, 구현 대기
- **상태**: D-025(하이브리드 방식), D-026(document_sources 테이블) 설계 결정 완료. 구현 시작 전.

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
- [ ] 2.6 Confluence MCP Client 연동 — 검색 기반 임포트 (D-016, I-010)
- [ ] 2.7 Confluence MCP Client 연동 — 트리 탐색 임포트
- [ ] 2.8 Confluence MCP Client 연동 — 내 문서 임포트

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
- [x] 7.4 그래프 추출 시 전체 문서 처리 — map-reduce 방식 (I-014)
- [x] 7.5 쿼리 확장(Query Expansion) — HyDE 적용 (I-016)
- [x] 7.6 문서 분류기 입력 범위 확대 (I-017)
- [x] 7.7 크로스-문서 엔티티 병합 (I-018, I-003)

### Phase 8: 배포
- [ ] 8.1 패키징 및 사내 배포
- [ ] 8.2 초기 설정 마법사 (대시보드 내)

### Phase 9: 추가 컨텍스트 소스 — Git 코드 기반 멀티에이전트 문서 생성
- [ ] 9.1 `document_sources` 테이블 추가 (code_doc ↔ git_code 연결, D-026)
- [ ] 9.2 `ingestion/git_repository.py` — Git repo clone/pull, 상품별 스코핑, 변경 감지
- [ ] 9.3 config에 `sources.git` 섹션 추가 — 상품 정의, 카테고리 프롬프트, 에이전트별 엔드포인트 (D-028, D-029)
- [ ] 9.4 Coordinator Agent 구현 — 전체 파이프라인 조율 (D-027)
- [ ] 9.5 Worker Agent 구현 — Level 1 파일 요약 + Level 2 디렉토리 문서 (D-027)
- [ ] 9.6 Category Agent 구현 — Level 3 상품×카테고리별 관점 문서 (D-027, D-028)
- [ ] 9.7 원본 코드 저장 (git_code) + document_sources 연결 (D-025, D-026)
- [ ] 9.8 code_doc → 기존 파이프라인 연결 (chunker → embedder → graph_extractor)
- [ ] 9.9 증분 처리 — git diff 기반 변경 디렉토리만 재처리
- [ ] 9.10 GitHub webhook 기반 자동 동기화
- [ ] 9.11 커밋 히스토리 / PR 리뷰 수집 및 컨텍스트화

### Phase 10 (후속): 추가 소스 확장
- [ ] 10.1 Jira API 연동 — 티켓, 요구사항-코드 연결
- [ ] 10.2 DB 스키마 수집 — DDL 스냅샷, 도메인 모델 그래프화
- [ ] 10.3 API 명세 (OpenAPI/Swagger) 자동 파싱

## 마지막 업데이트
- 일시: 2026-04-02
- 내용: 멀티에이전트 기반 코드 컨텍스트 구축 설계 완료.
  - D-025: 코드→LLM 문서 생성 + 원본 코드 하이브리드 방식 채택
  - D-026: `document_sources` 연결 테이블로 code_doc ↔ git_code 추적
  - D-027: 멀티에이전트 3계층 (Coordinator → Product/Worker → Category)
  - D-028: 상품 × 카테고리 매트릭스 문서 체계 (config 프롬프트 기반 확장)
  - D-029: 에이전트별 모델 계층화, 전체 엔드포인트 방식 통일
  - Phase 9 상세 로드맵 수립
  - 세션 문서: `.context/sessions/2026-04-02_추가-컨텍스트-소스-검토.md`
