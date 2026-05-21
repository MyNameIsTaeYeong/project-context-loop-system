# 측정 결과 진단 — hit 0.65 / recall 0.63 / precision 0.13 / MRR 0.52 / nDCG 0.54

## 1. 절대 수치 해석

| 메트릭 | 값 | 해석 |
|--------|------|------|
| **hit@5 = 0.65** | 35% 의 질의에서 정답이 top-5 에 **아예 없음** | 검색 ceiling 문제 (가장 큰 문제) |
| **recall@5 = 0.63** | 정답 doc 의 63% 만 top-5 로 회수 | hit 와 비슷 — 정답이 들어오면 잘 들어오지만 들어올 확률 자체가 65% |
| **precision@5 = 0.13** | top-5 평균 0.65 건 정답 = top-5 의 87% 가 오답 | 골드셋의 평균 정답 doc 수가 작은 듯 (보통 1~2 개). 정상 |
| **MRR = 0.52** | 첫 정답의 평균 등수 ≈ 1.92 | **정답이 들어오기만 하면 빠르게 등장** (1~2등). 랭킹 자체는 작동 |
| **nDCG@5 = 0.54** | 종합 랭킹 품질 | 보통 |

**핵심 패턴**: "**정답이 top-5 에 들어오면 잘 정렬되지만(MRR), 35% 는 아예 안 들어옴**". 이건 ranking 문제가 아니라 **recall ceiling 문제**.

## 2. R3 향상이 미미한 — 6가지 가설

R3 가상 질문 임베딩의 목적은 query/key 형태 정합으로 cosine 거리를 가깝게 만드는 것. baseline 대비 향상이 미미한 원인 가설:

### 가설 A — 가상 질문이 **실제로 인덱싱 안 됐다** (가장 먼저 확인할 것)

`enable_question_indexing=True` 기본이지만, 다음 중 하나면 실질적으로 OFF:
- `llm_client` 가 process_document 에 전달되지 않음 → 가상 질문 단계 스킵
- 모든 문서가 `InputTooLargeError` 로 폴백
- LLM 응답이 JSON 파싱 실패로 빈 dict 반환 (`stats.llm_failed=True`)
- 재인덱싱이 안 됨 — 기존 청크는 R3 코드 적용 전에 만들어진 그대로

**증상**: baseline 과 R3 가 거의 동일한 recall → vector_store 에 view="question" 엔트리가 0건이거나 매우 적을 가능성.

**점검 1줄**:
```python
# vector_store 의 view 분포 확인
from collections import Counter
all_entries = vector_store.collection.get(include=["metadatas"])
Counter(m.get("view") for m in all_entries["metadatas"])
# 기대: {"body": N, "meta": N, "question": K} — K 가 0 이거나 매우 작으면 가설 A 확정
```

### 가설 B — 가상 질문 톤이 **실제 사용자 query 와 정합 안 됨**

현재 프롬프트 톤 ("AuthService 는 어떻게 토큰을 검증하나요?" 같은 격식 자연 질의) 이 골드셋 query 와 다른 스타일이면 cosine 거리가 안 가까워진다.

골드셋 query 예시 패턴별 R3 효과:
| 질의 스타일 | 예시 | R3 가상 질문과 정합? |
|------------|------|---------------------|
| 자연 질의 (격식) | "결제 시스템의 인증 흐름은 어떻게 되나요?" | ✅ 가깝다 |
| 자연 질의 (비격식) | "결제 인증 어떻게 함?" | △ 어휘는 같지만 종결 어미 다름 |
| 키워드 나열 | "결제 인증 토큰 검증" | ❌ 명사구 vs 문장 — 거리 멀어짐 |
| 영문 혼용 | "auth flow 어떻게 동작?" | △ |
| 도메인 약어 | "SSO 토큰 만료" | ❌ 풀네임 우선 정책으로 약어 못 잡음 |

**점검**: 골드셋 query 의 몇 건을 샘플링해서 톤을 분류. 자연 질의 비중이 50% 미만이면 가설 B 확정.

### 가설 C — top-K cutoff 가 좁아 R3 효과가 묻힘

R3 dedup 정책이 `logical_chunk_id` → `document_id` 로 바뀌었다. 두 가지 상반된 영향:
- **이전**: 같은 doc 의 여러 청크가 top-5 점유 → top-5 unique doc 수 ↓ → recall 손해
- **R3**: top-5 = 5 개 unique doc → recall 향상되어야 함

만약 baseline (origin/main) 측정 시에도 dedup 이 logical_chunk_id 라면, R3 측정에서는 doc dedup 으로 차이가 나야 함. 그런데 차이가 거의 없다면:

- 동일 문서가 top-K 를 점유하는 케이스 자체가 적었다 (이미 baseline 이 다양한 doc 으로 채워져 있었음)
- 또는 doc dedup 이 retrieval 결과 변화에 큰 영향을 못 줌

**점검**: baseline 과 R3 의 retrieved_doc_ids 비교. 80%+ 가 동일하면 dedup 변화 영향 미미 → R3 의 핵심 효과는 **가상 질문 (가설 A/B)** 에 달려 있음.

### 가설 D — baseline 자체가 **시스템 한계 (ceiling)** 에 가까움

35% 질의의 정답이 top-5 에 안 들어오는 원인이 다음 중 하나라면 R3 가 못 잡음:
- 정답 doc 본문에 query 키워드가 거의 없음 (의미 매칭으로도 잡기 힘듦)
- 정답이 그래프 채널로만 매칭되는데 그래프 채널이 약함
- 골드셋 정답이 잘못 매겨짐 (false positive)
- doc 자체가 인덱스에 없음 (위 체크 1 누락 가능)

**점검**: `recall@5=0` 인 질의들 (= top-5 에 정답 0건) 의 query 와 source_document_id 를 보고 정답 doc 의 본문을 직접 확인.

### 가설 E — 골드셋 분포가 R3 의 강점과 어긋남

R3 의 가상 질문은 **정의/방법** 질의에서 큰 효과 예상. 골드셋이 다른 모드(키워드 룩업, 코드 위치, 그래프 탐색) 가 다수면 R3 효과 안 보임.

**점검**:
```bash
python -c "
import yaml
gold = yaml.safe_load(open('tests/eval/gold_set.yaml'))
from collections import Counter
modes = Counter(it.get('difficulty', '?') for it in gold['items'])
src = Counter(it.get('source_type', '?') for it in gold['items'])
print('difficulty:', modes)
print('source_type:', src)
print('query 샘플:')
for it in gold['items'][:5]: print('  -', it['query'])
"
```

### 가설 F — 비교가 **공정한 baseline 이 아니었음**

baseline 측정 시점에 인덱싱 상태가 origin/main 코드의 실제 동작과 일치했는가:
- vector_store 와 graph_store 가 R3 전 상태로 재인덱싱 되었는가? (R3 적용 후 vector_store 에는 view="question" 엔트리가 남아 있을 수 있음)
- 골드셋 자체가 둘에 대해 동일한 데이터 상태 가정인가?

만약 baseline 측정 시 vector_store 가 R3 인덱싱 상태였다면, "baseline 코드 + R3 인덱스" 라는 혼합 상태를 측정한 것 → R3 효과가 baseline 에 새 leakage.

**점검**: baseline 측정 전 `vector_store.delete_by_document` 전체 호출 후 origin/main 코드로 모든 문서 재인덱싱. 또는 별도 데이터 디렉토리 사용.

## 3. 우선순위 점검 액션 (5분 이내)

```bash
# A. vector_store 에 view='question' 엔트리가 실제로 있나
python -c "
from pathlib import Path
from collections import Counter
from context_loop.storage.vector_store import VectorStore
vs = VectorStore(Path('~/.context-loop/data').expanduser())
vs.initialize()
res = vs.collection.get(include=['metadatas'])
print('전체:', len(res['ids']))
print('view 분포:', Counter(m.get('view', '?') for m in res['metadatas']))
print('document_id 별 question view:')
from itertools import groupby
qmetas = [m for m in res['metadatas'] if m.get('view') == 'question']
by_doc = Counter(m.get('document_id') for m in qmetas)
for d, c in by_doc.most_common(10):
    print(f'  doc_id={d}: {c} 질문')
"
```

→ 결과 해석:
- view='question' 가 **0 건** 이면 → 가상 질문 미생성 (가설 A). pipeline 호출 또는 LLM 응답 실패. 로그 확인.
- 일부 doc 에만 있음 → 부분 실패. `InputTooLargeError` 또는 `llm_failed` 분포 확인.
- 모든 doc 에 있음 → 가설 A 기각, B/C/D/E 점검으로.

## 4. R3 효과가 진짜 있는지 / 없는지 분리하는 4가지 추가 측정

### 4-1. top-K 확장 측정

```bash
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml \
    --top-k 10 --no-judge --output _workspace/eval/r3_top10.jsonl
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml \
    --top-k 20 --no-judge --output _workspace/eval/r3_top20.jsonl
```

→ top-20 에서도 recall < 0.8 이면 시스템 한계 (가설 D). top-20 에서 recall 0.9+ 이면 ranking 문제로 R3 의 효과 영역이 아래에 묻혀 있을 가능성.

### 4-2. per-query breakdown

`_workspace/eval/r3.jsonl` 의 각 row 를 보고:
- `recall@5=0` 인 질의들만 추려서 query, relevant_doc_ids, retrieved_doc_ids 검토
- baseline 과 비교 → R3 가 어떤 질의에서 새 정답을 찾았고 어떤 질의에서 못 찾았는지

```bash
python -c "
import json
base = [json.loads(l) for l in open('_workspace/eval/baseline.jsonl')]
r3   = [json.loads(l) for l in open('_workspace/eval/r3.jsonl')]
delta = []
for b, r in zip(base, r3):
    d = r['recall@5'] - b['recall@5']
    if d != 0:
        delta.append((d, b['query'], b['retrieved_doc_ids'], r['retrieved_doc_ids']))
delta.sort()
print('R3 가 손해 본 케이스:')
for d, q, br, rr in delta[:5]: print(f'  Δ={d:+.2f}  {q[:60]}')
print('R3 가 이득 본 케이스:')
for d, q, br, rr in delta[-5:]: print(f'  Δ={d:+.2f}  {q[:60]}')
"
```

→ 이득 케이스가 정의/방법 질의인지, 손해 케이스가 키워드 룩업인지 패턴 분석.

### 4-3. 가상 질문 vs 본문 임베딩 cosine 직접 측정

특정 query 와 정답 doc 의 본문/가상 질문 임베딩 거리를 비교:

```bash
# query="결제 인증 흐름은?" 같은 1건에 대해
# - body 임베딩 거리
# - meta 임베딩 거리
# - question 임베딩 거리
# 어느 view 가 가장 가까운지 확인
```

→ question view 가 가장 가까우면 R3 메커니즘은 동작 — 골드셋 분포 문제 (가설 E). question view 거리가 body 거리보다 멀면 가상 질문 톤 문제 (가설 B).

### 4-4. include_graph=True 추가 측정

벡터만으로 한계라면 그래프 채널 결합으로 ceiling 돌파 가능:

```bash
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml \
    --top-k 5 --no-judge --include-graph \
    --output _workspace/eval/r3_with_graph.jsonl
```

→ 그래프 결합으로 hit/recall 이 크게 오르면 ceiling 은 벡터 채널 한계. R3 의 의미는 vector-only 효과 한정.

## 5. 진단 결과에 따른 조치 옵션

| 진단 | 조치 |
|------|------|
| 가설 A (view=question 0건) | 로그 확인 → process_document 호출 경로 점검, 재인덱싱 |
| 가설 B (톤 불일치) | 프롬프트 보강 — 자연 질의 + 키워드 나열 혼합, 영문/약어 강제 포함 |
| 가설 C (dedup 영향 0) | 다른 효과 없음 — 가상 질문 채널이 효과 주체. A/B 점검에 집중 |
| 가설 D (시스템 ceiling) | 그래프 채널 강화, 또는 retrieval 알고리즘 변경 (BM25 결합 등) |
| 가설 E (골드셋 분포 어긋남) | 정의/방법 질의 위주 sub-gold-set 만들어서 그쪽 효과만 측정 |
| 가설 F (불공정 baseline) | 데이터 디렉토리 분리 후 재측정 |

## 6. 가상 질문 자체의 품질 직접 확인 — 대시보드 활용

이미 PR #61 마지막 커밋으로 청크 탭에 가상 질문이 노출됨. 대시보드에서 5~10개 문서를 직접 보고:

- 질문이 의미 있는가? (본문에 답이 명확히 있는가)
- 질문 톤이 골드셋 query 와 비슷한가
- 빠진 표현이 있는가 (약어, 키워드 명사구 등)

가상 질문 품질이 낮으면 프롬프트 수정 → 재인덱싱 → 재측정.

## 7. 솔직한 진단

> **결과를 보면 가설 A (가상 질문 미생성/일부만 생성) 가능성이 가장 높습니다.** 가상 질문이 의도대로 인덱싱되었다면 정의/방법 질의에서는 visible 한 향상이 있어야 합니다. **먼저 vector_store 의 view 분포부터 확인하는 것을 가장 강하게 권합니다.**

> 가설 A 가 기각되면 (=question 엔트리가 정상 등록됨), 두 번째 가능성은 **가설 E (골드셋 분포)** — 골드셋이 R3 의 강점인 정의/방법 질의를 충분히 다루지 않으면 평균 향상이 noise 에 묻힙니다. per-query breakdown 으로 확인합니다.

> 그래도 효과가 없다면 **가설 B (톤 불일치)** — 프롬프트를 보강하여 키워드 명사구·약어·영문 혼용 등 다양한 표현을 추가합니다.

## 한 줄 요약

> hit 0.65 는 "35% 의 질의에서 아예 못 찾는다" 는 신호 — recall ceiling 문제. R3 가 향상이 없다면 **(1) vector_store 의 view=question 엔트리 수 확인 → (2) per-query breakdown → (3) 가상 질문 톤/품질 직접 검토** 순으로 진단하세요. 가설 A·E·B 가 가장 가능성 높습니다.
