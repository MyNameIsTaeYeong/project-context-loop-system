# 이슈 및 TODO 트래커

구현 중 발견된 이슈, 미결정 사항, 개선 아이디어를 기록한다.

## 미해결

### I-003: 엔티티 병합 테이블 스키마 미정
- 여러 문서에서 동일 엔티티가 등장할 때 병합 기준과 테이블 구조 상세 설계 필요
- Phase 3.6 시작 전까지 결정 필요

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

### I-012: HTML→Markdown 변환 시 테이블·매크로 손실
- `confluence.py:42-89`의 정규식 기반 변환기에서 `<table>`, `<tr>`, `<td>` 태그가 모두 strip되어 텍스트만 남음
- Confluence 매크로(`<ac:structured-macro>` 등 info/warning/code 패널)가 완전 무시됨
- 중첩 목록(`<ul>` 안의 `<ul>`)이 flat하게 변환됨
- MCP 쪽은 markdownify(D-017)로 전환했으나, REST API 쪽은 미전환
- **영향**: 원본 데이터 품질이 전체 파이프라인 퀄리티의 상한선을 결정하므로, 가장 우선적으로 개선 필요
- **개선 방향**: `markdownify` 또는 `beautifulsoup4` 기반 파서로 교체 + Confluence 전용 매크로 전처리 로직 추가

### I-013: 청킹 시 문서 구조(헤딩) 미활용
- `chunker.py`에서 `\n\n`(빈 줄) 기준으로만 단락을 분리함
- `# 섹션 제목`과 본문이 서로 다른 청크로 분리되어 청크가 맥락을 잃음
- 메타데이터에 `chunk_index`와 `title`만 저장 — 해당 청크가 속한 **섹션 헤딩 경로**가 없음
- chunk_size=512, overlap=50이 모든 문서에 고정 적용
- **개선 방향**: 마크다운 헤딩 기반 계층적 청킹, 각 청크에 상위 헤딩 경로(예: "프로젝트 개요 > 아키텍처 > 백엔드") 메타데이터 첨부

### I-014: 그래프 추출 시 콘텐츠 절삭 (앞 4000자만 사용)
- `graph_extractor.py:109`에서 `content[:max_content_chars]`로 앞 4000자만 LLM에 전달
- 긴 문서의 후반부 엔티티/관계가 모두 누락됨
- Confluence 문서는 도입부가 목차/배경이고 핵심 정보가 후반에 있는 경우가 많음
- **개선 방향**: 청크 단위 그래프 추출 후 병합(map-reduce), 또는 문서 요약 후 추출

### I-015: 컨텍스트 조립 시 재랭킹·필터링 부재
- 벡터 검색 결과에 유사도 threshold가 없어 관련 없는 청크도 top-K에 포함됨
- 벡터 검색 + 그래프 탐색 결과가 단순 concat으로 병합 — 중복/모순 정보의 우선순위 조정 없음
- Cross-encoder reranker가 없어 bi-encoder의 정밀도 한계를 극복하지 못함
- **개선 방향**: 유사도 threshold 도입(cosine sim < 0.3 제외), cross-encoder reranker 추가, 벡터+그래프 결과 가중 병합

### I-016: 쿼리 전처리 및 확장 부재
- 사용자 쿼리가 그대로 임베딩되어 검색에 사용됨
- 쿼리 확장(Query Expansion)이 없어 동의어 문서를 놓침 (예: "배포 절차" ↔ "릴리즈 프로세스")
- HyDE(Hypothetical Document Embedding) 등 검색 품질 향상 기법 미적용
- 대화 히스토리 유지 없이 매 쿼리가 독립적으로 처리됨
- **개선 방향**: LLM 기반 쿼리 확장 또는 HyDE, 대화 이력 기반 컨텍스트 유지

### I-017: 문서 분류기가 처음 2000자만 사용
- `classifier.py:60`에서 `content[:2000]`만 보고 chunk/graph/hybrid를 결정
- 문서 전체 구조를 파악하지 못해 잘못된 분류 발생 가능 (앞부분은 서술형이지만 뒤에 아키텍처 다이어그램이 있는 경우 등)
- **개선 방향**: 문서 전체의 구조적 특성을 요약 후 분류, 또는 여러 구간 샘플링

### I-018: 크로스-문서 엔티티 병합 로직 미구현
- `graph_store.py:104-120`에서 동일 엔티티가 문서마다 별도 노드로 생성됨
- 주석에 "논리적 병합"이라 되어 있지만 실제 병합 로직 없음
- 문서 간 관계 연결이 끊겨 그래프 탐색 범위가 단일 문서로 제한됨
- I-003과 관련됨
- **개선 방향**: entity_name + entity_type 기준 병합 테이블, 또는 임베딩 기반 유사 엔티티 자동 병합

## 해결됨

### I-001: 웹 프레임워크 최종 선택 → FastAPI + Jinja2 + HTMX
- 2026-03-11 결정: FastAPI + Jinja2 + HTMX + Alpine.js + Pico CSS
- 커스터마이징 자유도 높고 MCP 서버와 같은 프로세스 실행 가능

### I-002: 그래프 시각화 라이브러리 → vis.js
- 2026-03-11 결정: vis.js (CDN)
- 네트워크 그래프에 특화, 구현 공수 적음, 기본 인터랙션(줌/드래그/클릭) 제공
