# 사용자 요구사항 기준선 (3차 — 골드셋 생성·평가 병렬화)

**일시**: 2026-05-18 (3차)
**이전 작업**: `_workspace_prev2_20260518_143515/` — 2차(graph 인덱싱 강건). `_workspace_prev_20260518_105045/` 는 1차 보관 (PR #51 에 제외됨, 워크트리에만 존재).
**대상**: `scripts/build_synthetic_gold_set.py` (생성), `scripts/eval_search.py` (평가).

## 배경

현재 두 스크립트 모두 항목 단위 직렬 루프로 동작한다:

- `build_synthetic_gold_set.py:391` chunk 모드 `for i, chunk in enumerate(sampled):` — 청크당 LLM 3+회
- `build_synthetic_gold_set.py:589` graph 모드 동일 구조
- `eval_search.py:605` `for i, item in enumerate(gold.items):` — 항목당 검색+임베딩+선택 judge

async/await 인프라(LLMClient, EmbeddingClient, MetadataStore, VectorStore, GraphStore)는 이미 갖춰져 있으나, 메인 루프가 직렬이라 LLM 호출이 시리얼 병목. 300 항목 골드셋 ≈ 직렬 15분 → 동시성 8 → ~2분 예상.

임베딩은 이미 배치 처리됨(`_embed_graph_item_descriptions`, `aembed_with_client`). LLM 직렬화만 남은 병목.

## 만족해야 할 조건

### R1. 항목 단위 병렬 처리
- 생성: chunk 모드·graph 모드 모두 항목(청크/subgraph) 단위 동시 처리
- 평가: 골드 항목 단위 동시 처리
- 동시성 수준은 CLI 옵션으로 사용자 제어 가능 (rate limit 대응)

### R2. 결정성·재현성 보존
- 같은 시드 + 같은 코퍼스 + 같은 동시성 설정 → 같은 결과
- 동시성이 다르면 LLM 호출 타이밍은 달라도 **결과 골드셋의 항목 순서·id·내용은 동일**해야 한다
- GoldItem.id 부여를 사전 인덱스 기반으로 (현 `f"q{len(items)+1:04d}"` 는 append 순서 의존 — 비결정)

### R3. 백워드 호환
- `--concurrency=1` (기본) 시 현 동작과 사실상 동일 (로그·결과·순서)
- 기존 CLI 옵션·골드셋 YAML 스키마 변경 없음
- 메타데이터에 `concurrency` 기록 (재현 디버그용)

### R4. Rate Limit·에러 분리
- LLM endpoint 의 동시 호출 수가 cap 으로 통제되어야 함
- 한 항목 실패가 전체 작업을 죽이지 않아야 함 — exception 분리 + 보고

## 비기능 요구사항

- **테스트**: 기존 `tests/test_eval/` 통과 + 병렬화 동작에 신규 테스트 (결정성 회귀, 동시성 cap 동작, exception 격리)
- **LLM 호출 mock**: 테스트는 실제 endpoint 없이 통과해야 함
- **로그 가독성**: 직렬 진행률(`[3/100]`)이 순서대로 안 찍힐 수 있음. 완료 카운터 또는 그대로 둠 — 사용자 결정
- **SQLite read concurrency**: aiosqlite 의 동시성 한계 고려. 필요 시 store read cache 또는 동시성 적정값 추천
- **메모리**: 1000+ 항목 시 동시 in-flight 다수가 메모리 폭주하지 않도록 semaphore 자연 제한

## 비목표 (Out of Scope)

- 멀티 프로세스 (asyncio 만 사용. ProcessPoolExecutor 등 도입 안 함)
- LLM rate limit 자동 추정/적응형 동시성 (사용자가 명시적으로 N 지정)
- 다중 골드셋 간 병렬화 (`for gold_path in gold_paths:`) — 골드셋 내부 병렬화로 충분, 골드셋 단위 로그·집계 가독성 유지
- 임베딩 호출 추가 병렬화 (이미 배치)
- 진행률 막대 라이브러리 도입 (tqdm 등 — 의존성 비대)

## 핵심 의사결정 후보 (designer 결정)

analyst 분석 후 designer 가 결정:

1. **동시성 제어 메커니즘** — `asyncio.Semaphore(N)` vs `asyncio.gather` 단순 + 청크 batch vs `aiometer` 같은 외부 라이브러리. (외부 의존성 회피 권장)
2. **CLI 옵션 이름·기본값** — `--concurrency N` (기본 1) vs `--parallel N` vs 별도 환경변수. 생성·평가 양쪽 일관성
3. **id 사전 부여 구현** — sampling 직후 인덱스 → id 매핑 dict, 또는 항목별 함수가 idx 를 인자로 받기
4. **exception 격리 전략** — `asyncio.gather(return_exceptions=True)` + 결과 분리 vs 항목별 try/except 후 정상 결과만 수집
5. **로그 정책** — 진행률을 시작 시점에 찍을지(`[start 5/100]`), 완료 시점에 찍을지(`[done 5/100]`), 둘 다인지
6. **SQLite read cache** — 평가 측에서 `_fetch_source_text` 등이 같은 chunks 를 반복 조회. 사전 일괄 적재가 가치 있는가
7. **graph 모드 distractor 풀** — 현재 청크/subgraph 처리 함수가 `distractor_pool` 을 참조. 동시 변경 없으므로 read-only 공유 OK. 명시 필요
8. **stats 카운터** — 동시 증가 → race. local 누적 후 일괄 머지 (analyst 가 확인)
9. **다중 골드셋 평가 측 store 재사용** — 골드셋 간 직렬은 유지하되, 각 골드셋의 동시성이 store connection 을 share 해도 안전한지
10. **Judge LLM 의 동시 호출 안전성** — Generator/Judge 두 client 가 같은 endpoint 면 동시성 cap 이 share 되어야 하는지 (별도 cap)

## 산출물

1. `_workspace/01_analysis.md` (analyst — 현재 직렬 지점 정밀 매핑, 공유 상태·race 후보 식별, async 인프라 가용성 확인)
2. `_workspace/02_design.md` (designer — 10개 결정 + 패턴 + CLI + 테스트 전략)
3. `_workspace/03_implementation.md` + 실제 코드 (implementer — Semaphore 패턴, id 사전 부여, exception 격리, 신규 테스트)
