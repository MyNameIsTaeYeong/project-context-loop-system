# 2026-04-28 그래프 컨텍스트 고도화 — Extraction Unit + 본문 의미 관계 추출

## 배경

사용자 보고: "지금 그래프 데이터는 질의 시 유의미한 컨텍스트로 보이지 않아요."
진단 결과 6가지 원인:

1. Confluence 그래프가 link_graph(`outbound_links`)뿐 — 본문 의미 그래프 부재
2. 관계 의미 소실 (`references` 류만, depends_on 같은 진짜 의미 X)
3. 엔티티 병합 키 불안정 (이름 기반)
4. 섹션/스페이스/시간 축 부재
5. 그래프 탐색 결과 렌더링 빈약
6. LLM 플래너 스키마 인식 얕음

이 세션에서 (1)(2)(6)을 해결, (4)(5)는 차기 작업으로 분리.

## 결과 — 8개 PR 누적

브랜치: `claude/improve-graph-context-veDSQ`

| PR | 커밋 | 내용 |
|---|---|---|
| PR-1 | `9be0a2d` | ExtractionUnit 빌더 — 본문 기반 추출용 1500토큰 입자도 단위 |
| PR-2 | `b1ee655` | `chunks.section_index` 컬럼 + 마이그레이션 — chunk ↔ unit 조인 키 |
| PR-3 | `1516b33` | 결정론 본문 추출기 (`body_extractor`) + pipeline 통합 |
| PR-3a | `5f15029` | Option A — bold/table 헤더 기본 OFF (노이즈 감축) |
| PR-4 | `2a54998` | LLM 본문 추출기 (`llm_body_extractor`) — 도메인 의미 관계 |
| perf | `266bc82` | `_collect_units` ~7500배 단축 (lru_cache + own_body 캐시) |
| 통합 | `cbd7693` | `process_document` 호출 체인에 `llm_client` 전파 (4 사이트) |
| fix | `bd52f5a` | reasoning 모델 thinking 모드 비활성화 — 빈 응답 픽스 |
| PR-5 | `f0a7c6d` | `graph_vocabulary` 단일 출처 + planner 시스템 프롬프트 갱신 |

테스트 누적 +90건 (extraction_unit 21 + body 25 + llm_body 21 + vocab 7 +
planner 신규 2 + chunker/metadata/pipeline 회귀 보강).

## 핵심 설계 결정

### 1. ExtractionUnit — 추출 전용 입자도 (Phase 2 듀얼 그래뉼러리티)
- 벡터 검색 청크(512 토큰)와 분리된 추출 단위(target=1500/max=2400 토큰)
- 후위 순회로 `merged_tokens` 계산 → top-down 응축/분할/흡수 4규칙
- 안정적 `section_id = f"{document_id}:{section_index}"` 부여
- breadcrumb로 상위 문맥 주입 (`# 문서: ...`, `## 위치: A > B > C`, 머리말)
- 기존 chunker는 그대로, ExtractionUnit은 본문 추출 파이프라인에서만 사용

### 2. 결정론 본문 추출기 — 보수적 기본값 (Option A)
- **API 엔드포인트** (`POST /v1/payments`) ON ✅
- **Jira 키** (`PROJ-123`) ON ✅
- **굵게 강조 용어** OFF ❌ (작성 컨벤션 의존, 노이즈 다수)
- **표 헤더** OFF ❌ ("Method"/"필수" 같은 추상 헤더는 도메인 엔티티 아님)
- 본질적 의미 관계는 LLM 추출기가 담당, 결정론은 구조적 신호만

### 3. LLM 본문 추출기 — 어휘 고정 + 검증
- 9개 의미 관계 타입: `depends_on`, `implements`, `calls`, `owned_by`,
  `supersedes`, `has_part`, `uses`, `provides`, `documented_in`
- 7개 엔티티 타입: `system`, `module`, `api`, `concept`, `policy`, `person`, `team`
- 어휘 외 출력 / 끝점 누락 / self-loop 모두 검증 단계에서 드롭
- 단위 격리 (한 unit 실패가 다른 unit 영향 없음)
- 비용 게이트: `min_unit_tokens=200`, `skip_split_overlap_parts=True`
- **opt-in**: `PipelineConfig.enable_llm_body_extraction=True` + `llm_client` 둘 다 전달 시에만 발동

### 4. graph_vocabulary 단일 출처
- 추출기들의 어휘를 한 곳에 카탈로그화 (`processor/graph_vocabulary.py`)
- 12 entity_types + 19 relation_types + 7 intent → relation 매핑
- 추출기 어휘 ⊆ vocabulary 강제 테스트 → 미래 누락 자동 검출
- planner 시스템 프롬프트가 `_render_system_prompt()`로 동적 렌더 → LLM이
  어휘 의미를 알게 됨

### 5. 호출 체인 통합
- `process_document(llm_client=...)` 옵셔널 파라미터
- 4개 사이트에 전파: `coordinator.py`(Git), `mcp_sync.py`(Confluence MCP),
  `web/api/documents.py`(수동 재처리), `web/api/git_sync.py`
- DI: `app.state.llm_client` → `Depends(get_llm_client)`

## 트러블슈팅 — 빈 응답 → thinking 모드

증상: `process_document` 흐름에서 LLM 본문 추출 응답이 빈 상태로 옴
(`/api/chat` 은 정상 동작).

진단:
- Qwen3 등 reasoning 모델은 응답 전 `<think>...</think>` 사고 블록 생성
- JSON 추출 프롬프트 + `max_tokens=1024` → 사고에 토큰 모두 소진
- `extract_json` 의 `<think>` 정규식 스트립 후 빈 문자열만 남음

해결 (`graph_search_planner` 가 동일 문제로 이미 적용한 처방):
- `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`
- `max_tokens` 기본값 1024 → 2048

## 트러블슈팅 — `_collect_units` 13초 → 1.8ms

`cProfile` 결과: 14초 중 13.6초가 `requests.get` (HTTPS).
`tiktoken/load.py:read_file` 가 vocabulary 를 매 호출마다 재다운로드 시도
(환경에 따라 캐시 무력화). 357 섹션 문서 기준 357 × 38ms = 13.6초.

해결:
- `chunker._get_tokenizer` 에 `functools.lru_cache(maxsize=8)` 적용
- 부수적으로 `_Node.own_body` 캐시 + `_render_subtree`/`_dfs` 단일 walk 통합

13447ms → 1.8ms (~7500배). 전체 테스트 시간 28s → 16s.

## 어중간한 영역 — cross-document 관계 출처 보존

발견: `nx.DiGraph` 가 같은 (src, tgt) 사이 엣지 1개만 허용 → 두 문서가 같은
`Auth Service --depends_on--> Token Validator` 를 추출하면:
- SQLite: 2 rows (제대로)
- NetworkX: 1 edge (last-write-wins, 출처 메타데이터 손실)
- "여러 문서가 동의하는 관계" 신호 소실

차기 PR 후보 (Option A): 엣지에 `source_document_ids: set[int]` 누적 보존.

## 미해결 — 차기 작업

### 우선순위 1: 검색 품질 테스트 (다음 세션 시작점)
지금까지의 추출 인프라가 실제 검색 품질을 얼마나 끌어올렸는지 측정.
- 같은 질의 셋을 인덱싱 전후/`enable_llm_body_extraction` ON/OFF 비교
- 메트릭: 정답 적중률, 그래프 활용 빈도, focus_relations 정확도, 토큰 비용
- 사람 평가 30~50개 샘플 (정확/누락/환각)
- 결과로 PR-3 bold/table 토글 재평가, LLM 추출 ROI 결정
- 측정 도구가 없으면 추측만 누적되므로 **다음 세션 1순위**

### 우선순위 2: 검색 결과 렌더링 개선
- 그래프 결과에 본문 스니펫 (`section_index` ↔ chunks 조인 활용 — PR-2 키 사용처)
- 경로 서술 (multi-hop reasoning narrative)
- 출처 섹션 anchor 링크 노출
- 노드별 연결 차수로 중요도 표시

### 우선순위 3: 운영 가시성
- 그래프 통계 CLI/엔드포인트 (entity_type 분포, 평균 차수, drop 비율)
- LLM 추출 결과 샘플 덤프

### 선택 (낮음)
- 메타 노드화 (author/space/label)
- 섹션 노드화 (PR-2 `section_index` 직접 활용)
- LLM 호출 게이트 (분류 기반 선택적 호출)
- 관계 cross-doc 출처 보존 (Option A)

## 처음 진단 6가지 원인 대비

| 원인 | 상태 |
|---|---|
| 1. 본문 의미 그래프 부재 | ✅ PR-3 + PR-4 |
| 2. 관계 의미 소실 | ✅ PR-4 |
| 3. 엔티티 병합 키 불안정 | ⚠️ 부분적 (이름 기반 그대로) |
| 4. 섹션/스페이스/시간 축 부재 | ❌ 미진행 |
| 5. 검색 결과 렌더링 빈약 | ❌ 미진행 |
| 6. LLM 플래너 스키마 인식 얕음 | ✅ PR-5 |

## 회귀

- 환경 의존 실패 (Jinja2/Starlette 호환, openai 미설치)는 baseline 동일 — 제 변경과 무관
- 그 외 311 통과, ruff/mypy 깨끗

## 커밋 그래프

```
f0a7c6d feat(graph_search_planner): 어휘 단일 출처화 + 시스템 프롬프트 갱신 (PR-5)
bd52f5a fix(llm_body_extractor): reasoning 모델 thinking 모드 비활성화 — 빈 응답 수정
cbd7693 feat: process_document 호출 체인에 llm_client 주입
266bc82 perf(extraction_unit): _collect_units 처리 시간 ~7500배 단축
2a54998 feat(processor): LLM 본문 추출기 — 도메인 의미 관계 추출 (PR-4)
5f15029 chore(body_extractor): 강조/표 헤더 시그널 기본 비활성화 (Option A)
1516b33 feat(processor): 결정론적 본문 추출기 + pipeline 통합
b1ee655 feat(storage): chunks 테이블에 section_index 컬럼 추가 + pipeline 전달
9be0a2d feat(processor): Extraction Unit 빌더 추가 — 본문 기반 그래프 추출 입력 단위
```
