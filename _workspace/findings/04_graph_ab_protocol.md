# 그래프 검색 A/B 측정 프로토콜 — "개선 여부"를 객관적으로 판정하기 (S0-3)

> 전제: S0-1(표면 tier 분리), S0-2(그래프 CI), S0-4(비교 가드) 패치가 적용된 상태.
> 감사 결론(`SUMMARY.md`)상 그래프 메트릭은 **동일 인덱스 위에서 retriever/planner
> 변경의 방향성**만 신뢰 가능하다. 절대값 보고·인덱싱 변경 A/B·임베딩 교체에는 쓰지 말 것.

## 0. 무엇을 측정하나 (브리지 예시)

코드↔지식 브리지(PR #77, `_resolve_seed_nodes` seed-union)가 그래프 검색을 개선했는지
판정한다. 브리지는 **검색 코드만** 바꾸고 인덱스·임베딩·골드셋은 건드리지 않으므로 시나리오
(1)에 해당 → S0 패치 후 측정 가능.

- **baseline arm**: 브리지 머지 직전 커밋 (PR #77 머지커밋 `9860953`의 첫 부모).
- **treatment arm**: 브리지 포함 (현재 origin/main).
- 두 arm은 **같은 인덱스 DB, 같은 골드셋, 같은 τ, 같은 임베딩 모델, 같은 planner seed**.

## 1. 골드셋 동결 생성 (1회만, 두 arm 공용)

cross-source 항목(R8)이 있어야 브리지가 자극된다. graph + cross-doc 모드로 생성하고
generator≠judge 분리(편향 회피)·evidence 임베딩 박기·충분한 N(R6: 그래프 항목 ≥150 권장).

```bash
uv run python scripts/build_synthetic_gold_set.py \
    --output _workspace/eval/gold_graph_ab.yaml \
    --include-graph-questions \
    --enable-cross-doc --source-types confluence_mcp git_code \
    --embed-graph-evidence true \
    --generator-endpoint <GEN_URL> --generator-model <GEN_MODEL> \
    --judge-endpoint <JUDGE_URL> --judge-model <JUDGE_MODEL> \
    --target-graph-questions 150
```

- `--enable-cross-doc --source-types confluence_mcp git_code`: 문서↔코드가 끊긴(disjoint
  doc) 엣지를 시드로 → 질의가 한쪽 표기(문서식 "Auth Service"), 정답 엔티티는 양끝(코드 FQN
  포함). 이게 브리지를 자극하는 핵심.
- generator/judge를 **시스템 LLM과 다른 family**로 (R9 Channel C: fall-through 시 generator=
  시스템 planner LLM이 될 수 있음). 분리 모델 ID가 메타데이터에 기록되는지 확인.
- 생성 후 **골드셋 파일을 동결**(재생성 금지) — 두 arm이 글자 그대로 같은 정답으로 채점돼야 함.

> 한계 고지(R3 self-fitting): 그래프 골드 정답은 인덱스 노드에서 유래하므로 T1이 자명 통과한다.
> 브리지 측정에서는 이게 오히려 유효 — "정답 노드가 회수됐는가"가 곧 브리지의 효과(0→1)이고,
> self-fitting은 "회수되면 매칭 보장"일 뿐이다. 단 절대 recall 값은 신뢰하지 말 것(방향만).

## 2. 인덱스 고정

두 arm이 **같은 인덱스 DB**를 보게 한다(재인덱싱 금지 — 브리지는 검색측 변경이라 인덱스 불변).
임베딩 모델·`graph_match_threshold`(τ, 기본 0.65)를 두 run에서 동일하게 고정한다.

## 3. 두 arm 평가 실행 (검색 코드만 교체)

```bash
# baseline (브리지 직전 코드)
git checkout 9860953^   # PR#77 머지 직전
uv run python scripts/eval_search.py \
    --gold-set _workspace/eval/gold_graph_ab.yaml \
    --label baseline --include-graph \
    --graph-match-threshold 0.65 --planner-seed-base 42 \
    --out _workspace/eval/run_baseline.json

# treatment (브리지 포함)
git checkout claude/graph-eval-reliability   # 또는 origin/main
uv run python scripts/eval_search.py \
    --gold-set _workspace/eval/gold_graph_ab.yaml \
    --label treatment --include-graph \
    --graph-match-threshold 0.65 --planner-seed-base 42 \
    --out _workspace/eval/run_treatment.json
```

- `--planner-seed-base` 고정 → 그래프 플래너 결정성.
- 같은 `--graph-match-threshold` → S0-4 비교 가드가 `graph_comparison_invalid` 안 띄워야 정상.
- 같은 임베딩 클라이언트 config.

## 4. 판정 기준 (객관성)

1. **1차 지표 = `graph_recall_surface@k` (T1–T3만)**. T4 임베딩 false-positive(R2)에 오염되지
   않은 결정론 매칭. treatment − baseline 델타를 본다.
2. **유의성 = 그래프 CI 비중첩** (S0-2). 두 arm의 `graph_recall_surface@k` 95% CI가 겹치지
   않을 때만 "개선"으로 판정. 겹치면 "유의차 없음".
3. **보조 = tier 분포**(`graph_match_tiers`). treatment에서 `embedding`(T4) 비중이 급증했다면
   recall 상승이 fuzzy 매칭 탓일 수 있으니 의심. surface 델타가 양(+)이어야 진짜 개선.
4. **비교 유효성**(S0-4): `graph_comparison_invalid` 플래그가 false인지 확인(τ·임베딩·인덱스
   동일). true면 비교 무효 → 조건 맞춰 재실행.
5. **표본**(R6): 그래프 항목 N이 충분한지(≥150 권장). 작으면 CI가 넓어 유의차가 안 나옴 —
   "개선 없음"이 아니라 "측정력 부족"임에 유의.

## 5. 보고 양식 (방향성만)

| 메트릭 | baseline [95% CI] | treatment [95% CI] | 판정 |
|---|---|---|---|
| graph_recall_surface@5 | … | … | CI 비중첩 시 ↑개선 / 겹침 시 = |
| graph_hit_surface@5 | … | … | … |
| graph_recall@5 (T4 포함) | … | … | 보조 — surface와 괴리 크면 T4 의심 |
| T4 hit 비중 | … | … | treatment 급증 시 경고 |

> **금지**: 위 recall 절대값을 "그래프 검색 정확도 N%"로 외부 보고하지 말 것(self-fit 골드셋).
> 오직 "브리지가 동일 인덱스에서 그래프 회수를 유의하게 늘렸다/아니다"의 방향 결론만 도출.

## 6. (선택) compare_runs 자동화
S1로 `scripts/compare_runs.py`(두 run.json의 메트릭 차이 + CI 중첩 여부 표 출력)를 추가하면
4장 판정을 자동화할 수 있다. 현재는 수동 비교.
