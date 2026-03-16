# 구현 진행 상황

## 현재 단계
- **Phase**: 6 (질의 및 고도화) → **완료**
- **Step**: Phase 7 진입 준비
- **상태**: Phase 5 (MCP Server) + Phase 6 (채팅 인터페이스 + 출처 표시) 완료

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

### Phase 7: 배포
- [ ] 7.1 패키징 및 사내 배포
- [ ] 7.2 초기 설정 마법사 (대시보드 내)

## 마지막 업데이트
- 일시: 2026-03-16
- 내용: Phase 5 + Phase 6 구현 완료 — MCP Server (4개 Tool + context_assembler), 대시보드 채팅 인터페이스 (RAG 파이프라인 + 출처 표시), 테스트 13개 추가, 전체 109개 통과
