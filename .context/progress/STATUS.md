# 구현 진행 상황

## 현재 단계
- **Phase**: Phase 9 — 추가 컨텍스트 소스 (Git 코드 기반 컨텍스트 구축)
- **Step**: 9.8++ AST 기반 정적 코드 추출 + 메서드 단위 청킹 (D-036, D-037)
- **상태**: LLM 기반 코드 그래프 추출을 AST 기반 정적 분석으로 완전 전환. (1) D-036: `ast_code_extractor.py` 신규 — Python ast 모듈 + Go/Java/TS/JS 키워드+중괄호 매칭으로 심볼/import 추출. LLM 호출 제로, 파일당 수 ms. `pipeline.py`에서 `source_type == "git_code"` 시 AST 경로 분기. 임베딩 텍스트(이름+시그니처+docstring)와 저장 문서(전체 코드) 분리로 검색 정확도 향상. (2) D-037: 클래스 → 메서드 단위 청킹. `parent_name`/`parent_signature`로 소속 클래스 추적. 클래스→메서드 `contains` 관계 자동 생성. 전체 흐름: git clone → store git_code → AST 추출(extract_code_symbols) → 심볼 청크(to_chunks) + import 그래프(to_graph_data) → 벡터DB + GraphStore. 테스트 33개 + 기존 141개 통과. 다음: 증분 처리(9.9) — git diff 기반 변경 파일만 재처리.

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
- [x] 9.1 `document_sources` 테이블 추가 (code_doc ↔ git_code 연결, D-026)
- [x] 9.2 `ingestion/git_repository.py` — Git repo clone/pull, 상품별 스코핑, 변경 감지
- [x] 9.3 config에 `sources.git` 섹션 추가 — 상품 정의, 카테고리 프롬프트, 에이전트별 엔드포인트 (D-028, D-029)
- [x] 9.4 Coordinator Agent 구현 — 전체 파이프라인 조율 (D-027)
- [x] ~~9.5 Worker Agent 구현~~ — Worker Agent 제거 (D-034)
- [x] ~~9.6 Category Agent 구현~~ — Level 2/3 제거 (D-033)
- [x] 9.7 원본 코드 저장 (git_code) + document_sources 연결 (D-025, D-026, D-030)
- [x] 9.8 git_code → 기존 파이프라인 직접 연결 (hybrid 고정, Classifier 건너뜀) (D-034)
- [ ] 9.9 증분 처리 — git diff 기반 변경 파일만 재처리
- [ ] 9.10 GitHub webhook 기반 자동 동기화
- [ ] 9.11 커밋 히스토리 / PR 리뷰 수집 및 컨텍스트화

### Phase 10 (후속): 추가 소스 확장
- [ ] 10.1 Jira API 연동 — 티켓, 요구사항-코드 연결
- [ ] 10.2 DB 스키마 수집 — DDL 스냅샷, 도메인 모델 그래프화
- [ ] 10.3 API 명세 (OpenAPI/Swagger) 자동 파싱

## 마지막 업데이트
- 일시: 2026-04-16
- 내용: git_code를 LLM 기반에서 AST 기반 정적 추출로 전환 (D-036, D-037).
  - **배경**: D-035의 LLM 기반 코드 그래프 추출에서 빈번한 타임아웃 발생. 코드는 구조화된 데이터이므로 LLM 추론이 불필요 — AST 정적 분석으로 100% 정확한 추출 가능.
  - **D-036**: `ast_code_extractor.py` 신규 모듈
    - Python: `ast` 모듈 기반 정확한 파싱
    - Go/Java/TS/JS: 키워드 + 중괄호 매칭
    - 추출: 함수/클래스/메서드/struct/interface 심볼 + import 관계
    - `pipeline.py`: `source_type == "git_code"` 시 AST 경로 분기, LLM 호출 완전 우회
    - `coordinator.py`: `storage_method_override` `"graph"` → `"hybrid"` — 벡터 검색 활성화
    - 임베딩/저장 분리: 임베딩(이름+시그니처+docstring) vs 저장(전체 코드)
  - **D-037**: 메서드 단위 청킹
    - 클래스 내부 메서드를 개별 청크로 분할, `parent_name`/`parent_signature`로 소속 추적
    - `section_path`: `file > class > method` 계층 구조
    - Go 리시버 메서드, Java/TS/JS 클래스 메서드 모두 지원
    - 클래스→메서드 `contains` 관계 자동 생성
    - 메서드 없는 클래스(데이터 클래스)는 기존처럼 단일 심볼 유지
  - **전체 흐름**: git clone → store git_code → extract_code_symbols() → to_chunks() + to_graph_data() → 벡터DB + GraphStore
  - **테스트**: ast_code_extractor 33개 + 기존 141개 통과
- 이전 (2026-04-15): 코드 전용 그래프 스키마 + graph-only 처리 (D-035).
  - **D-035**: git_code를 코드 전용 프롬프트로 graph-only 처리
  - **변경 사항**:
    - `graph_extractor.py`: `source_type` 파라미터 추가. `_CODE_SYSTEM_PROMPT` + `_select_prompts()` — git_code는 코드 전용 엔티티(function, class, struct, interface, package, module, endpoint, error_type, constant, type_alias) + 관계(calls, imports, implements, contains, returns, depends_on, raises, receives) 프롬프트 사용
    - `pipeline.py`: `doc["source_type"]`을 `extract_graph()`에 전달
    - `coordinator.py`: `storage_method_override`를 `"hybrid"` → `"graph"`로 변경 — 코드 chunking 건너뜀
  - **기존 문서**: 변경 없음 — `source_type`이 `git_code`가 아니면 기존 프롬프트 사용
  - **GraphStore/검색**: 변경 없음 — entity_type은 자유 문자열이므로 코드/문서 엔티티 자연 공존
  - **전체 흐름**: git clone → store git_code → pipeline(graph_extractor with 코드 전용 프롬프트, chunking 건너뜀)
  - **테스트**: 380개 비-web 테스트 전체 통과 (신규 8개: 코드 프롬프트 선택, 코드 엔티티/관계 파싱, map-reduce)
- 이전 (2026-04-13): Phase 9.7 원본 코드 저장 (git_code) + document_sources 연결 구현 완료.
  - **핵심 구현** (`ingestion/coordinator.py`)
    - `ProductResult`에 `files: list[FileInfo]`, `repo_url: str` 필드 추가
    - `run_and_store()`에서 `store_git_code()`로 원본 코드 → `git_code` 문서 DB 저장
    - `git_code_map` (relative_path → document_id) 구축 후 document_sources 연결:
      - code_summary ↔ git_code: file_summaries의 relative_path로 1:1 매칭
      - code_doc ↔ git_code: `_collect_git_code_ids()`로 source_directories 기반 매칭
    - `run()`은 side-effect-free 원칙 유지 (D-031)
  - **원본 코드 첨부** (`mcp/context_assembler.py`)
    - `include_source_code: bool = False` 파라미터 추가 (opt-in)
    - `_extract_doc_ids()`: 검색 결과에서 document_id 추출
    - `_fetch_and_format_source_code()`: document_sources를 따라 git_code 원본을 마크다운 코드 블록으로 포맷
    - 중복 git_code 제거 (seen_source_ids), 언어 힌트 자동 추출
  - **검증 스크립트** `scripts/run_git_code_store.py` (기존 `run_phase97_test.py`에서 리네임)
    - 기존 스크립트(`run_worker_agent.py`, `run_category_agent.py`)와 동일한 `--config`/`-c` + `--full-pipeline` 패턴 적용
    - 3가지 모드: 샘플 레포(기본), `--full-pipeline`(config yaml), `--repo`(로컬 레포)
    - Mock Agent 기반 E2E 검증 9섹션: run() 전달, git_code 저장, code_summary 연결, code_doc 연결, 역방향 조회, 멱등성, _collect_git_code_ids, 원본 코드 첨부, DB 통계
  - **테스트**: coordinator 24개 + context_assembler 22개 전체 통과 (기존 418개 비-web 테스트 무회귀)
  - **설계 결정**: D-031 — git_code 저장과 document_sources 연결을 run_and_store()에서 수행
- 이전 (2026-04-09): Phase 9.6 Category Agent — 서버 과부하 대응 및 안정화.
  - **Map 배치 병렬 제어**: 무제한 병렬 → `asyncio.Semaphore(4)` 최대 4개 동시 실행
    - 초기: `asyncio.gather`로 전체 병렬 → 서버 "peer closed connection" 오류
    - 1차 대응: 직렬 처리로 전환 → 안정적이나 느림
    - 최종: 세마포어(4)로 제한된 병렬 → 속도와 안정성 균형
    - `max_concurrent_batches` 파라미터로 조정 가능
  - **스트리밍 기능 제거**: `EndpointLLMClient`의 `stream` 파라미터 및 `_complete_stream()` 삭제
    - `git_config.build_llm_client()`의 `stream` 파라미터 삭제
    - `scripts/run_category_agent.py`의 `stream=True` 호출 제거
    - 스트리밍 전용 테스트 8개 → 일반 응답 테스트 3개로 교체
  - **배치 크기 축소**: `max_chars_per_batch` 15000 → 8000 (prefill 타임아웃 방지)
  - 테스트 25개(category_agent) + 3개(llm_client) 전체 통과
- 이전 (2026-04-08): Phase 9.6 Category Agent 구현 완료 + Map-Reduce 타임아웃 대응.
  - **`ingestion/category_agent.py`** 신규: `LLMCategoryAgent` 클래스
    - Level 2 디렉토리 문서를 종합하여 카테고리별 관점 문서(Level 3) 생성
    - config의 카테고리 프롬프트를 system 프롬프트로 사용 (D-028)
    - orchestrator 엔드포인트(Opus급 고성능 모델) 사용 (D-029)
    - **글자수 기반 동적 배치 + Map-Reduce**: `max_chars_per_batch=8000` 기준으로
      디렉토리 요약을 배치로 분할. 배치 1개이면 단일 호출, 2개 이상이면
      Map(배치별 부분 문서 최대 4개 병렬 생성) → Reduce(부분 문서 종합) 2단계 처리
    - Map: max_tokens=8192, Reduce/단일: max_tokens=16384
    - Map 배치 일부 실패 허용, 부분 문서 1개만 남으면 Reduce 생략
    - 빈 입력 시 빈 문서 반환 (LLM 호출 안 함)
  - **`processor/llm_client.py`**: `EndpointLLMClient`에 `timeout` 파라미터 추가
    - 기본 600초, connect 10초. 대형 입력 처리 시 안전망 역할
    - `git_config.build_llm_client(agent, timeout=...)` 으로 에이전트별 설정 가능
  - **수동 테스트 스크립트** `scripts/run_category_agent.py`
    - `--input-dir` 모드: Worker 출력(_level2_summary.md)을 읽어 Category Agent 실행
    - `--full-pipeline` 모드: Git clone → Worker → Category Agent 순차 실행
    - `--categories` 옵션: 특정 카테고리만 선택 실행
    - 결과를 `scripts/output/{product}/category/{category}.md`에 저장
  - **Coordinator 수정 불필요**: 기존 `_run_category_agent()` 메서드가
    `CategoryAgentProtocol`을 통해 `LLMCategoryAgent`를 그대로 호출
  - 다음: 원본 코드 저장(9.7) — git_code DB + document_sources 연결
- 이전: Phase 9.5 Worker Agent 구현 + Coordinator 리팩토링 완료.
  - **`ingestion/worker_agent.py`** 신규: `LLMWorkerAgent` 클래스
    - Level 1 (파일 요약): worker LLM, max_tokens=4096, `enable_thinking=False`
    - Level 2 (디렉토리 문서): synthesizer LLM, max_tokens=8192, `enable_thinking=False`
    - 관점 중립 사실 요약. 개별 파일 실패 허용. `asyncio.Semaphore(max_concurrent_files=5)` 병렬 처리.
    - 장문 절삭(`max_file_tokens` 초과 시)
    - 테스트 13개 전체 통과
  - **Coordinator `_process_repository` 리팩토링 (D-030)**
    - `sync_repository()` 제거 → `clone_or_pull()`만 호출 (불필요한 git_code DB I/O 제거)
    - scopes를 직접 순회하며 상품별 `collect_files([scope])` → `_process_product()` 호출
    - `parse_product_scopes(clone_dir=...)` 전달 버그 수정
  - **수동 테스트 스크립트** `scripts/run_worker_agent.py`
    - `--local-dir` 모드: 로컬 파일 직접 분석
    - `--full-pipeline` 모드: Git clone → 상품별 Worker 실행
    - 결과를 `scripts/output/{product}/{directory}/` 하위에 마크다운 파일로 저장
    - `_level1_{filename}.md` (파일별 요약) + `_level2_summary.md` (디렉토리 종합)
  - **설계 결정**: D-030 — git_code DB 저장을 Phase 9.7로 분리
  - 다음: Category Agent(9.6) 구현
- 이전: Phase 9.4+ — `scope_analyzer.py` config 기반 전면 전환 (I-026, I-027 해결).
  - **LLM 기반 → config 기반 전환**: 956줄 → ~120줄. 2-pass 아키텍처, 레이어 감지, LLM 프롬프트 전량 삭제
  - **새 아키텍처**: config에 상품명 정의 → `resolve_product_paths()`가 BFS로 레포 전체 순회 → 파일명 토큰 매칭
  - **핵심 기능**: `_plural_variants()` 복수형 생성, `_filename_matches_product()` 경계 인식 토큰 매칭
  - **exclude 패턴**: fnmatch glob으로 tests/, vendor/ 등 제외 가능
  - **`parse_product_scopes()` 연동**: paths 미지정 시 자동 탐지, 수동 paths 우선
  - **테스트 스크립트**: `scripts/run_product_paths.py` — config yaml 읽어 zero-argument 실행
  - 테스트 24+4개 전체 통과
- 이전: Phase 9.4 — `ingestion/coordinator.py` 구현 완료 (D-027).
  - `CoordinatorAgent`: 전체 파이프라인 조율 (config 검증 → git sync → 상품별 분류 → Worker/Category 디스패치)
  - `WorkerAgent`/`CategoryAgentProtocol`: Protocol 기반 인터페이스 (Phase 9.5/9.6에서 LLM 구현)
  - `asyncio.Semaphore`로 max_concurrent_workers 동시성 제어
  - `store_directory_summary()`: code_summary 저장 (Level 2, 멱등)
  - `store_category_document()`: code_doc 저장 + document_sources 연결 (Level 3)
  - `run_and_store()`: 파이프라인 실행 + DB 저장 통합 메서드
  - 테스트 14개 (Mock Worker/Category Agent로 E2E 검증) — 전체 통과
- 이전: Phase 9.3 — `ingestion/git_config.py` 구현 완료 (D-028, D-029).
  - `GitSourceConfig` 타입 안전 dataclass (sources.git 전체 설정 파싱)
  - `CategoryConfig`: 카테고리 정의 + source_id 생성 (상품×카테고리 매트릭스)
  - `ProcessingConfig` + `LLMEndpointConfig`: 에이전트별 엔드포인트 설정
  - `resolve_endpoint()`: 에이전트별 → 글로벌 llm.* 폴백 해소 (D-029)
  - `build_llm_client()`: EndpointLLMClient 팩토리
  - `validate()`: 필수 설정 검증 (레포/카테고리/엔드포인트)
  - `load_git_source_config()`: Config 인스턴스에서 GitSourceConfig 로드
  - 테스트 26개 — 전체 통과
- 이전: Phase 9.2 — `ingestion/git_repository.py` 구현 완료.
  - Git repo clone/pull (asyncio subprocess)
  - 상품별 스코핑 (config paths/exclude glob 패턴)
  - git diff 기반 증분 변경 감지
  - git_code 문서 저장 (content_hash 기반 생성/갱신/무변경 판별)
  - 삭제 파일 처리, 디렉토리별 그룹핑 (Worker Agent 배정 단위)
  - 테스트 31개 (단위 16 + 통합 15) — 전체 통과
