# 2026-07-10 search_context 검색 과정 분석 + 그래프 탐색 ablation 스위치 구현

## 배경

사용자 요청 흐름 (분석 → 비판적 검토 → 실측 준비 순으로 발전):
1. "search_context 시 검색 과정을 상세히 알려주세요" — 파이프라인 전체 추적
2. HyDE 평균 임베딩이 왜 동의어·약어를 반영하는지 개념 설명
3. `plan_graph_search` 의 쿼리 임베딩 기반 서브그래프 추출 과정 설명
4. `execute_graph_search` 를 예시로 단계별 설명
5. "과정이 너무 복잡한데 각 층이 의미 있나? 단순화 가능한가?" — 비판적 검토
6. ablation 평가 개념 설명 후 "돌려볼 수 있나요?" — 실행 준비 작업

## 분석 결과 요약 (세션 전반부)

### search_context 파이프라인 (src/context_loop/mcp/context_assembler.py)

쿼리 임베딩(HyDE 옵션) → 벡터 검색(6배 over-fetch + document_id dedup +
similarity threshold) → 리랭킹·그래프 탐색 병렬(asyncio.gather) →
parent-document 치환 → 그래프 연결 문서 첨부(_search_graph_sourced_chunks,
벡터가 못 찾은 순수 추가분만) → 원본 소스 코드 첨부 → 섹션 조립.

### execute_graph_search 복잡도 평가 (핵심 결론)

복잡도는 설계가 아니라 R1→R2→R3 패치의 지층:
- 시드 이름 임베딩 fallback / always-on 보강(0.6/top-3) / 최후 폴백(0.5/top-5)
  3층은 전부 "LLM 플래너가 틀리는 것"을 수습하는 층 — 임베딩 검색으로 수렴
- priority 정렬·_natural_description·GraphRelationRef 는 평가 메트릭
  (MRR/NDCG/T4) 지원용 — 검색 품질과 무관하게 프로덕션 코드에 침투
- search_steps/focus_relations 경로는 프로덕션 호출자 없는 죽은 코드
  (플래너 모듈 자신 + 테스트에서만 참조 — grep 으로 확인)
- 플래너의 고유 기여는 ① should_search 게이팅 ② 관계 방향성 인식뿐인데,
  ②의 결과물(target_relations)은 끝점 시딩에만 쓰이고 방향성 필터링은 안 함

**단순화 가설**: "쿼리 임베딩 → 유사 엔티티 top-k → 1-hop 확장" 만으로
현행 대비 크게 밀리지 않을 것 (always-on 보강 도입 경위 자체가 근거).
단, 눈감고 단순화하지 말고 ablation 으로 실측 후 결정하기로 함.

## 결과 — 커밋 1건

브랜치: `claude/search-context-process-w9pa4z` (main 1ea72db 에서 분기)

| # | 커밋 | 내용 |
|---|---|---|
| 1 | `219cb49` | 그래프 탐색 ablation 모드 추가 (--graph-ablation) |

변경 파일:
- `src/context_loop/processor/graph_search_planner.py` —
  `execute_graph_search` 에 게이트 3개: `require_targets`(빈 계획 허용),
  `enable_query_boost`, `enable_seed_fallback`
- `src/context_loop/mcp/context_assembler.py` — `GRAPH_ABLATION_MODES`
  상수(full/no-planner/no-boost/no-seed-fallback), `graph_ablation` 배관
  (`assemble_context_with_sources` → `_rerank_and_search_graph` →
  `_search_graph_with_llm`). **no-planner 는 플래너 LLM 호출 없이 빈
  GraphSearchPlan 으로 execute 에 진입, llm_client=None 에서도 동작**
- `scripts/eval_search.py` — `--graph-ablation` CLI 플래그,
  `evaluate_one`/`_process_item` 배관, config_summary 에 `graph_ablation` 기록
- 테스트 +6건 (planner 게이트 4건, assembler no-planner 2건)

검증: 대상 2개 파일 79 passed. 전체 스위트는 변경 전후 실패 목록 동일
(기존 test_web 등 7건 — jinja2 이슈, 본 변경과 무관). 실서비스 기본
동작("full")은 불변.

## 미완료 / 다음 단계

**ablation 실측이 미실행 상태.** 원격 컨테이너에는 골드셋(eval/*.yaml,
git 미커밋)·인덱스(~/.context-loop/data)·모델 엔드포인트가 없어 실행 불가.
사용자 로컬 환경에서:

```bash
python scripts/eval_search.py -g eval/gold_set.yaml --planner-seed-base 2000 \
    --graph-ablation full             --label abl_full
# 동일하게 no-planner / no-boost / no-seed-fallback 3회 더
```

읽는 법:
- full 대비 no-planner 하락폭 = LLM 플래너의 실제 기여도.
  하락 미미 + elapsed_ms 감소 크면 → 플래너 제거(최소 버전) 채택 근거
- no-boost 폭락 시 → 임베딩 보강이 주력이라는 뜻
- 변동성 우려 시 --gold-set-glob 으로 mean/std 확보

후속 결정 대기:
1. ablation 결과 해석 → 단순화 방향 확정 (최소 버전 vs 플래너 유지+fallback 통합)
2. 단순화 채택 시: search_steps/focus_relations 죽은 코드 삭제,
   priority 정렬·description 합성의 평가 측 이동 검토
