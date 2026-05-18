# Level 2/3 제거 + Worker Agent 제거 (D-033, D-034)

- **일시**: 2026-04-14
- **범위**: 멀티에이전트 문서 생성 계층 전면 제거. Level 2/3 제거(D-033) → Worker Agent 제거 + git_code 직접 파이프라인(D-034)
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

## 효과 (D-033)

- **LLM 비용**: 모델 3개(worker/synthesizer/orchestrator) → 1개(worker)
- **복잡도**: 멀티에이전트 3계층 → 단일 Worker Agent
- **품질**: 중복 정보 감소, 그래프 기반 관계 추출이 더 정확

---

## D-034: Worker Agent 제거 — git_code 직접 파이프라인 처리

### 배경

D-033 후 Worker Agent의 유일한 역할은 원본 코드를 LLM으로 요약(code_file_summary)하여 파이프라인에 넘기는 것. 검토 결과:

- **정보 손실**: LLM 요약은 함수 시그니처, 에러 처리, 엣지 케이스 등 세부 정보를 누락
- **그래프 추출 정확도 저하**: 요약에서 엔티티/관계를 추출하면 원본 코드 대비 정확도 낮음
- **불필요한 비용**: 파일당 LLM 호출 1회가 추가되지만 품질 향상 없음
- **Classifier 비결정성**: 같은 성격의 파일이어도 chunk/hybrid를 비일관적으로 판정

### 변경 내용

#### 1. worker_agent.py 삭제
- Worker Agent 전체 구현 삭제

#### 2. coordinator.py 전면 재작성
- `FileSummary`, `DirectorySummary` dataclass 제거
- `WorkerAgentProtocol`, `worker` 파라미터 제거
- `_process_product()`, `_run_worker()`, `store_file_summary()` 제거
- `_semaphore`, Worker dispatch 로직 제거
- `ProductResult` 단순화: `product`, `errors`, `files`, `repo_url`만 유지
- `PipelineResult` 단순화: `total_directories` 제거
- `run()`: git clone + 파일 수집만 수행
- `run_and_store()`: git_code 저장 → 변경분만 파이프라인 처리
- `_process_through_pipeline()`: `storage_method_override="hybrid"` 전달

#### 3. pipeline.py 수정
- `process_document()`에 `storage_method_override` 파라미터 추가
- 설정 시 LLM Classifier를 건너뛰고 지정된 방식으로 처리

#### 4. git_sync.py 정리
- Worker Agent 생성 블록 제거
- 문서 목록 파셜: `code_file_summary` → `git_code`로 변경

#### 5. 테스트 업데이트
- `test_worker_agent.py` 전체 삭제
- `test_coordinator.py` 재작성: 16개 테스트 (MockWorker 제거, 파이프라인 테스트 추가)

### 설계 결정

- **D-034**: Worker Agent 제거, git_code 직접 파이프라인 처리 (hybrid 고정)

### 테스트 결과

- 전체 비-web 테스트: 372개 전체 통과 (무회귀)

### 변경 파일

| 파일 | 변경 유형 |
|------|---------|
| `src/context_loop/ingestion/worker_agent.py` | 삭제 |
| `src/context_loop/ingestion/coordinator.py` | 전면 재작성 — Worker Agent 코드 전면 제거 |
| `src/context_loop/processor/pipeline.py` | 수정 — `storage_method_override` 파라미터 추가 |
| `src/context_loop/web/api/git_sync.py` | 수정 — Worker Agent 생성 제거 |
| `tests/test_ingestion/test_coordinator.py` | 재작성 — Worker 참조 제거, 파이프라인 테스트 추가 |
| `tests/test_ingestion/test_worker_agent.py` | 삭제 |

### 효과 (누적: D-033 + D-034)

- **LLM 비용**: 모델 3개(worker/synthesizer/orchestrator) → 0개 (파이프라인 내 graph_extractor LLM만 사용)
- **복잡도**: 멀티에이전트 3계층 → 단순 파이프라인 (git clone → store → pipeline)
- **품질**: 원본 코드에서 직접 엔티티/관계 추출 → 정보 손실 없음, 일관된 hybrid 처리
