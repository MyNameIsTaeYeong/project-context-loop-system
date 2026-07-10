# 절대 점수 신뢰화 프로토콜 (청크/문서)

검색 평가 시스템은 기본적으로 **"동일 환경에서 개선 전후 비교"** 에는 조건부로
신뢰할 수 있지만, **"절대 점수(예: Recall@5 = 0.82)를 외부에 인용하거나 운영 출시
게이트로 사용"** 하려면 추가 보장이 필요하다. 이 문서는 그 보장을 코드로 강제하는
프로토콜을 설명한다.

> 범위: 이번 프로토콜은 **청크/문서 메트릭**(recall/precision/mrr/ndcg @k)의 절대
> 신뢰화를 다룬다. 그래프·교차문서 메트릭의 절대 신뢰화는 별도 후속 작업이다.

## 절대 점수가 갖춰야 할 세 기둥

1. **재현성** — 같은 입력이면 누가 언제 재도 같은 점수. (Phase 1)
2. **타당성** — 점수가 주장하는 의미를 실제로 가짐(정답 동치 모델이 정확해 recall
   이 과소평가되지 않음). (Phase 3.5)
3. **앵커링 + 불확실성** — 점수가 "어느 코퍼스/골드셋/설정" 위에서 나왔는지 못
   박히고, 점추정이 아니라 신뢰구간을 동반. (Phase 0/4/5)

## 구성 요소

| 도구/플래그 | 역할 | 기둥 |
|---|---|---|
| 임베딩 시딩 그래프 검색 (LLM 플래너 제거) | 그래프 탐색이 결정적 → 그래프 증강이 청크 recall 에 주는 비결정성 원천 제거 | 1 |
| `eval_search.py --judge-seed-base` + sha256 기반 seed | Judge 점수 프로세스 독립 결정성 | 1 |
| `build_synthetic_gold_set.py --equivalence-detection` | 같은 답을 담은 동등 문서를 OR 그룹(`relevant_doc_groups`)으로 기록 → recall 과소평가 해소 | 2 |
| 인덱스/코퍼스 지문(`index_fingerprint`) | summary 에 vector/corpus/graph sha256 기록 | 3 |
| `eval_search.py --absolute-mode` | 재현성·비편향·앵커·표본 요건 강제 + 청크 메트릭 95% CI 동반 | 1·3 |
| `verify_frozen_benchmark.py` + `--frozen-benchmark` | 코퍼스/골드셋 드리프트 차단(anchor_verified) | 3 |
| `compare_runs.py` (인덱스 지문 동치성) | A/B 비교 시 같은 코퍼스에서 측정됐는지 검증 | 3 |

## `--absolute-mode` 강제 요건

`--absolute-mode` 로 실행하면 다음을 만족하지 못할 때 **비정상 종료**한다.

- `--judge-seed-base` 설정(Judge 재현성)
- `--judge-n-samples >= 3`(Judge 변덕 완화 — 중앙값 + 분산 보고)
- 비-self Judge(`--allow-self-judge` 없이) — 자기평가 편향 차단
- 인덱스 지문(`vector_store_sha256`) 비어 있지 않음(앵커링 가능)

그래프 탐색은 임베딩 시딩 기반의 결정적 검색이므로(LLM 플래너 제거) 별도
seed 요건이 없다 — 같은 인덱스 + 같은 쿼리 임베딩이면 같은 결과.

충족 시 summary 에 `absolute_mode: true` 와 청크 메트릭별 `metric_ci`
(`mean`/`ci_low`/`ci_high`/`n`, 95% bootstrap, seed=42)가 기록된다.

## 고정 벤치마크 생애주기

```bash
# (a) 동결 — 현재 인덱스/골드셋으로 manifest 생성
python scripts/verify_frozen_benchmark.py --benchmark eval/frozen/main --create

# (b) 인용 전 검증 — 드리프트 시 exit 2
python scripts/verify_frozen_benchmark.py --benchmark eval/frozen/main

# (c) 절대 점수 측정 — 앵커 검증 + CI 동반
python scripts/eval_search.py --gold-set eval/frozen/main/gold_set.yaml \
    --judge --judge-seed-base 1000 --judge-n-samples 3 \
    --absolute-mode \
    --frozen-benchmark eval/frozen/main --label absolute --output-dir eval/runs

# (d) 인덱스 변경(재인덱싱) 후에는 (a) 로 돌아가 재동결 + 재캘리브레이션
```

`--frozen-benchmark` 가 manifest 의 `gold_set_sha256` / `vector_store_sha256` /
`corpus_sha256` / `graph_store_sha256` 와 현재를 비교해 하나라도 다르면 실행을
거부한다(드리프트). 일치하면 summary 에 `anchor_verified: true` +
`benchmark_manifest_sha256` 가 기록되어, 그 절대 점수가 동결 벤치마크에 앵커링됐다는
기계검증 증거가 된다.

## 동등 정답(OR-동치) 자동 검출

질문은 문서 #42 에서 생성되지만 같은 답이 #99 에도 있으면, 검색기가 #99 를
회수해도 정답이어야 한다. 기존 uniqueness 게이트는 코퍼스 전역 검색이 아니라 LLM
판단 + 무관 샘플이라 진짜 동등 문서를 놓칠 수 있어 recall 이 과소평가될 수 있었다.

`--equivalence-detection` 은 질문 생성 후 정답 청크 임베딩으로 **코퍼스 전역 벡터
검색** → cosine 하한 + answer-containment(`is_answerable`) 검증을 통과한 문서를
`relevant_doc_groups` 의 OR 그룹으로 기록한다. 채점기(`_reduce_equivalence`)는 그룹
내 어느 문서를 회수해도 hit 로 친다.

```bash
python scripts/build_synthetic_gold_set.py --n-chunks 50 \
    --equivalence-detection --equivalence-top-m 3 \
    --output eval/frozen/main/gold_set.yaml
# 빌드 stats 의 equivalence_groups_added / non_unique_recovered 로 효과 확인
```

## 사용 가능 / 금지 매트릭스

| 의사결정 | 신뢰 |
|---|---|
| 동일 환경 큰 회귀 검출 | ✅ |
| 동일 환경 A/B 미세 개선 | ✅ (compare_runs + N≥10) |
| **청크/문서 절대 점수 인용·출시 게이트** | ✅ (`--absolute-mode` + `--frozen-benchmark`, anchor_verified=true 시) |
| 그래프·교차문서 절대 점수 | ⏳ (별도 후속) |

## 한계

- `AnthropicClient` Judge 는 seed 미지원 → Judge 점수 레이어의 완전 bit-identity 는
  불가. `--judge-n-samples >= 3` 의 중앙값 + CI 로 대체하며, 검색 레이어 메트릭은
  여전히 bit-identical.
- Judge seed(`--judge-seed-base`)는 **평가 경로 전용**이다. 절대 점수가 실서버
  동률 처리 순서를 100% 대변하지는 않는다(6배 over-fetch + 결정적 재정렬로
  top-k 영향은 무시 가능).
