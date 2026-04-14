# Level 2/3 문서 생성 제거 (D-033)

- **일시**: 2026-04-14
- **범위**: 3단계 문서 생성 계층에서 Level 2(code_summary), Level 3(code_doc)를 제거하고 Level 1(code_file_summary)만 유지
- **브랜치**: `claude/prepare-phase-9.8-NbTdP`

## 배경

3단계 문서 생성 계층의 가치를 검토한 결과:

- **Level 1 (code_file_summary)**: 파일별 LLM 요약 — 파이프라인(chunker → embedder → graph_extractor)에 의해 벡터 검색 + 그래프 탐색 가능. **유지**
- **Level 2 (code_summary)**: 디렉토리 내 파일 종합 — 같은 디렉토리 내 관계만 포착. 크로스-디렉토리 관계(더 가치 있음)는 Level 1의 그래프 추출 + 엔티티 병합(D-024)이 더 효과적. **제거**
- **Level 3 (code_doc)**: 카테고리별 관점 문서 — 고정 관점(5개)이 사용자 질의와 불일치 가능, 7x 정보 중복, 높은 LLM 비용(Opus급). RAG 안티패턴. **제거**

## 변경 내용

### 1. category_agent.py 삭제
- Level 3 전체 구현(LLMCategoryAgent, map-reduce) 삭제

### 2. worker_agent.py 단순화
- `synthesizer_llm` 파라미터 제거 (단일 `worker_llm`만 사용)
- `_synthesize_directory()` 메서드 제거
- `_DIR_SYNTHESIS_SYSTEM`, `_DIR_SYNTHESIS_TEMPLATE` 프롬프트 제거
- `process_directory()`: Level 1 요약만 수행, `document=""` 반환

### 3. coordinator.py 정리
- `CategoryDocument` dataclass 제거
- `CategoryAgentProtocol` 제거
- `category_agent` 파라미터 제거
- `store_directory_summary()`, `store_category_document()` 제거
- `_collect_git_code_ids()` 함수 제거
- `run_and_store()`: git_code + code_file_summary만 저장 + 파이프라인

### 4. git_sync.py 정리
- Category Agent 생성 블록 제거
- `synthesizer_llm`, `orchestrator_llm` 생성 제거 (worker_llm만 유지)
- `total_documents_generated` 참조 제거
- 문서 목록 파셜: `code_doc`, `code_summary` 타입 제거

### 5. 테스트 업데이트
- `test_category_agent.py` 전체 삭제
- `test_coordinator.py` 재작성: MockCategoryAgent 제거, Level 2/3 관련 테스트 제거, 파이프라인 테스트 업데이트 (call_count: 7 → 1)
- `test_worker_agent.py` 재작성: synthesizer LLM 제거, Level 2 테스트 제거

## 설계 결정

- **D-033**: Level 2/3 제거, Level 1만 유지

## 테스트 결과

- ingestion 테스트: 38개 전체 통과 (worker 11개 + coordinator 27개)
- 전체 비-web 테스트: 394개 전체 통과 (무회귀)

## 변경 파일

| 파일 | 변경 유형 |
|------|---------|
| `src/context_loop/ingestion/category_agent.py` | 삭제 |
| `src/context_loop/ingestion/worker_agent.py` | 수정 — synthesizer LLM + Level 2 합성 제거 |
| `src/context_loop/ingestion/coordinator.py` | 수정 — Level 2/3 관련 코드 전면 제거 |
| `src/context_loop/web/api/git_sync.py` | 수정 — Category Agent + synthesizer/orchestrator LLM 제거 |
| `tests/test_ingestion/test_category_agent.py` | 삭제 |
| `tests/test_ingestion/test_coordinator.py` | 재작성 — Level 2/3 참조 제거 |
| `tests/test_ingestion/test_worker_agent.py` | 재작성 — Level 2 관련 테스트 제거 |

## 효과

- **LLM 비용**: 모델 3개(worker/synthesizer/orchestrator) → 1개(worker)
- **복잡도**: 멀티에이전트 3계층 → 단일 Worker Agent
- **품질**: 중복 정보 감소, 그래프 기반 관계 추출이 더 정확
