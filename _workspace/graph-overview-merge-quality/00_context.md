# Graph Overview & Merge Quality — Run Context

## 사용자 요청 원문

> 웹 대시보드에 그래프 탭을 하나 만들어주세요. 그리고 그 그래프 탭에는 모든 그래프의 연결관계가 보이도록 해주세요. 그리고 각 엔터티에는 어떤 문서에서 추출되었는지를 확인할 수 있어야 합니다. 그리고 현재 그래프 엔터티가 의미론적으로 통합되고 있는지 알려주세요.

## 사용자 결정 사항 (clarification 답변)

- **분석 범위**: 진단 + 통합 품질 메트릭 도구화 (자동 병합은 하지 않음, 가시화/메트릭만)
- **노출 위치**: 상단 nav에 새 메뉴 (`/graph`)
- **가시화 전략**: 타입별 클러스터 요약 뷰 (entity_type별 클러스터 → 클릭 시 펼침)

## 실행 모드

- **하이브리드**: Phase A(analyst 단일 서브) → Phase B(backend + frontend 병렬 서브) → Phase C(QA 단일 서브)
- 초기 실행 (이전 산출물 없음)

## 작업 디렉토리

`_workspace/graph-overview-merge-quality/` 하위에 모든 산출물 작성.
파일 명명: `00_context.md`, `01_merge_diagnosis.md`, `02_backend.md`, `03_frontend.md`, `04_qa.md`

## 다른 하네스와 격리

본 하네스는 웹 대시보드 + 엔티티 통합 품질을 다룬다.
- `graph-search-diagnosis`: 검색 funnel — 영역 다름
- `indexing-improvement`: 추출 로직 자체 — 영역 다름
- `rag-eval-*`: 평가 시스템 — 영역 다름

본 하네스가 만진 코드만 다른 하네스 산출물과 격리해 작업한다.

## 참고 진입점

- 그래프 인프라: `src/context_loop/storage/graph_store.py`, `src/context_loop/storage/metadata_store.py`
- 웹 라우터 패턴: `src/context_loop/web/api/documents.py`, `src/context_loop/web/app.py`
- 단일 문서 그래프 탭: `src/context_loop/web/templates/partials/tab_graph.html`, `static/js/graph.js`
- 데이터 파일: `~/.context-loop/data/metadata.db` (SQLite)
