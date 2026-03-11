# 이슈 및 TODO 트래커

구현 중 발견된 이슈, 미결정 사항, 개선 아이디어를 기록한다.

## 미해결

### I-001: 웹 프레임워크 최종 선택 미결정
- FastAPI + Jinja2 vs Streamlit — 프로토타이핑 속도와 커스터마이징 유연성 간 트레이드오프
- Phase 4 시작 전까지 결정 필요

### I-002: 그래프 시각화 라이브러리 미결정
- vis.js vs D3.js — vis.js는 쉽고 빠르나 커스터마이징 한계, D3.js는 유연하나 구현 공수 큼
- Phase 4.3 시작 전까지 결정 필요

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

## 해결됨

(아직 없음)
