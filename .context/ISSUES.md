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

## 해결됨

### I-001: 웹 프레임워크 최종 선택 → FastAPI + Jinja2 + HTMX
- 2026-03-11 결정: FastAPI + Jinja2 + HTMX + Alpine.js + Pico CSS
- 커스터마이징 자유도 높고 MCP 서버와 같은 프로세스 실행 가능

### I-002: 그래프 시각화 라이브러리 → vis.js
- 2026-03-11 결정: vis.js (CDN)
- 네트워크 그래프에 특화, 구현 공수 적음, 기본 인터랙션(줌/드래그/클릭) 제공
