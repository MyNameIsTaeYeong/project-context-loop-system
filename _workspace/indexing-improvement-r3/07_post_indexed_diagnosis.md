# 가상 질문이 인덱싱됐는데도 향상이 없는 경우 — 정밀 진단

가설 A (미인덱싱) 기각. 남은 가설을 데이터로 빠르게 결정하는 4단계 진단.

## Step 1 — 매칭된 view 분포 (가장 결정적)

R3 의 핵심 가설: "가상 질문 view 가 사용자 query 와 더 가깝다." 이를 직접 검증:

```python
# 골드셋의 모든 query 에 대해 top-1 매칭된 view 가 무엇인지 집계
import yaml, asyncio, json
from collections import Counter
from pathlib import Path
from context_loop.storage.vector_store import VectorStore
from context_loop.processor.embedder import EndpointEmbeddingClient
from context_loop.config import load_config

cfg = load_config()
vs = VectorStore(Path(cfg.app.data_dir).expanduser())
vs.initialize()
embed = EndpointEmbeddingClient(
    cfg.processor.embedding_endpoint,
    cfg.processor.embedding_model,
)

async def main():
    gold = yaml.safe_load(open("tests/eval/gold_set.yaml"))
    views_top1 = Counter()
    views_in_top5 = Counter()
    for item in gold["items"]:
        q_emb = await embed.aembed_query(item["query"])
        results = vs.search(q_emb, n_results=20)
        if results:
            views_top1[results[0]["metadata"].get("view", "?")] += 1
        for r in results[:5]:
            views_in_top5[r["metadata"].get("view", "?")] += 1
    print("top-1 매칭 view:", dict(views_top1))
    print("top-5 매칭 view (누적):", dict(views_in_top5))

asyncio.run(main())
```

**결과 해석**:

| 시나리오 | top-1 view 분포 | 진단 |
|---------|---------------|------|
| `question` 이 50%+ | ✅ R3 메커니즘 동작 — 가상 질문이 query 와 가까움. **그런데 recall 향상이 없다** = 가상 질문이 같은 정답 doc 의 body 와 동일 doc 을 가리키고 있어 dedup 후엔 같은 결과 → **R3 의 가치는 같은 doc 의 다른 view 가 매칭되는 것 뿐, 새 doc 을 발견하는 것이 아님** |
| `question` 10% 미만 | ❌ body/meta 가 압도 — 가상 질문이 거의 매칭 안 됨 → **가설 B 확정 (톤 불일치)** |
| `question` 30~50% | 혼재 — 일부 질의에서만 효과 |

**핵심 통찰**: question view 가 매칭되어도 recall 향상이 없다면, 그 매칭 doc 이 어차피 body view 로도 잡혔다는 뜻. R3 의 가치는 "새 정답 doc 을 발견" 이 아니라 "같은 정답 doc 의 다른 view 를 찾는" 형태로 흡수됨. doc-level recall 에는 차이가 없다.

→ 이게 가장 가능성 있는 진짜 원인일 수 있다. **R3 가 doc-level metric 으로는 측정 불가**.

## Step 2 — 골드셋 query 톤 분류

R3 의 강점은 자연 질의 형태 query. 골드셋이 다른 패턴이면 R3 효과 안 보임:

```python
import yaml
gold = yaml.safe_load(open("tests/eval/gold_set.yaml"))
samples = [it["query"] for it in gold["items"][:20]]
for q in samples: print(f"  {q}")
print(f"\n총 {len(gold['items'])} 건")
```

**톤 분류 (직접 사람 눈으로)**:
- 자연 질의 ("X 는 어떻게 동작하나요?", "X 의 책임은 무엇인가요?") — R3 의 직접 타겟
- 키워드 명사구 ("X 동작", "X 책임") — R3 가 못 잡음 (질문 형태 vs 명사구)
- 코드 위치 ("X 함수는 어디", "X 클래스 정의") — 식별자 기반, AST + meta view 가 주력
- 약어/영문 ("SSO 흐름", "auth API") — 풀네임 우선 정책으로 약점

자연 질의 비중이 30% 미만이면 **가설 E (분포 불일치) 확정**. R3 효과가 묻혔을 가능성.

## Step 3 — Top-K 확장으로 ceiling 판별

```bash
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml \
    --top-k 10 --no-judge --output _workspace/eval/r3_top10.jsonl
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml \
    --top-k 20 --no-judge --output _workspace/eval/r3_top20.jsonl
```

| top-K 결과 | 진단 |
|-----------|------|
| top-20 recall < 0.75 | **가설 D (시스템 ceiling)** — vector 채널 자체의 한계. 35% 정답은 어떤 K 로도 못 잡음 |
| top-20 recall 0.85+ | ranking 문제 — 정답은 검색되지만 멀리 있음. R3 가 정답을 더 위로 못 끌어올림 |

## Step 4 — `include_graph=True` 결합 효과

```bash
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml \
    --top-k 5 --no-judge --include-graph \
    --output _workspace/eval/r3_with_graph.jsonl
```

그래프 결합으로 recall 0.85+ 도달하면 vector 채널은 약하지만 hybrid 가 정답 — R3 의 기여도는 hybrid 안의 component 로 평가해야 함.

## 솔직한 가능성 — R3 효과가 진짜 작을 수 있다

**가상 질문 임베딩이 항상 효과 있는 게 아닙니다.** RAG 업계에서 검증된 패턴이긴 하지만 다음 조건에서는 효과가 작거나 0:

1. **임베딩 모델이 이미 자연 질문 ↔ 서술문 정합을 잘함** — 좋은 임베딩 모델은 query/passage symmetric encoding 을 학습. 가상 질문 추가의 marginal gain 거의 0.
2. **본문 자체가 짧고 명확** — 사내 위키 문서는 보통 충분히 self-contained 라 본문 임베딩만으로 매칭 잘 됨. 가상 질문이 보강할 여지 없음.
3. **유사 doc 의 distractor 가 많음** — 검색이 어려운 이유가 doc 간 유사성이라면, query 형태 차이가 아니라 doc 내용 식별이 문제. 가상 질문은 이걸 해결 못 함.
4. **golden recall 의 cap 이 vector 채널이 아닌 다른 데 있음** — 키워드 누락, 그래프 정답, embedding 모델 한계 등.

특히 **사내 위키 + nomic-embed-text 류 임베딩** 조합이면 가상 질문의 ROI 가 낮을 가능성이 큽니다. R3 의 효과는 "정의/방법 자연 질의 사용자" 가 다수일 때 두드러집니다.

## 다음 결정 — 4가지 분기

진단 결과별 권장 액션:

### 분기 1 — Step 1 에서 question 매칭률이 높은데 recall 향상 없음
**진단**: R3 메커니즘 동작, 그러나 doc-level metric 으로 측정 불가 (같은 doc 의 다른 view 매칭).

**액션**:
- 평가 metric 을 **chunk-level recall** 으로 추가 (가상 질문이 매칭된 청크가 정답 청크인지) — 다만 골드셋이 doc-level 이라 별도 anchor/section 정답 필요
- 또는 **다른 강점** 측정 — 답변 정확성 (judge), 사용자 만족도 (사람 평가)
- R3 의 doc-level 기여가 정말 없으면, R3 의 임베딩 추가 비용 대비 ROI 평가 → 폐기 또는 유지 결정

### 분기 2 — Step 1 에서 question 매칭률 10% 미만
**진단**: 가설 B 확정 — 톤 불일치.

**액션**:
- 가상 질문 프롬프트 보강 — 자연 질의 + **키워드 명사구 + 약어 + 영문 혼용** 모두 생성 강제
- 또는 골드셋 query 자체를 R3 톤으로 보강 (양방향 정합)
- 재인덱싱 + 재측정

### 분기 3 — Step 2 에서 골드셋이 자연 질의 < 30%
**진단**: 가설 E 확정 — 분포 불일치.

**액션**:
- 자연 질의 subset 만 추려서 R3 효과 별도 측정
- 또는 골드셋에 자연 질의 보강 (사람 작성, R3 인덱싱 적용 전 LLM 사용)

### 분기 4 — Step 3/4 에서 시스템 ceiling 확인
**진단**: 가설 D 확정 — vector 채널 한계.

**액션**:
- R3 폐기 고려 — 가상 질문 임베딩으로 ceiling 못 돌파하면 복잡도만 추가
- 대신 hybrid retrieval (BM25 + dense + 그래프) 도입
- 또는 reranker 강화

## 솔직한 권고

> **현재 데이터만으로는 R3 의 효과가 "진짜 없음" 인지 "측정 한계" 인지 분리할 수 없습니다.** Step 1 (매칭된 view 분포) 가 결정적인 첫 데이터입니다 — 5분 안에 답이 나옵니다.

가능성을 솔직히 정리:

| 결과 | 확률 | 의미 |
|------|-----|------|
| question 매칭 0~10% | 30% | 톤 불일치 — 프롬프트 보강하면 회복 가능 |
| question 매칭 30~60%, recall 차이 없음 | **40%** (가장 가능성 큼) | **R3 메커니즘 동작하지만 doc-level metric 으로는 측정 불가**. 효과의 본질이 metric 과 misalign |
| question 매칭 다수 + recall 향상 | 10% | 측정/계산 오류 — 다시 확인 필요 |
| ceiling 한계 (top-20 도 ≤ 0.8) | 20% | R3 와 무관, vector 채널 자체 한계 |

가장 가능성 큰 시나리오 (40%) 는 **"R3 의 가치는 같은 doc 안에서의 매칭 품질 (어느 부분에 답이 있는지) 향상인데, 골드셋 metric 은 doc 단위 hit/recall 이라 그게 안 보이는 것"**. 이 경우 R3 의 진짜 효과는 답변 품질(judge) 이나 사용자 만족도 같은 다른 차원에서 봐야 합니다.

또는 R3 자체가 이 환경(사내 위키 + 사용된 임베딩 모델)에서 ROI 가 낮을 수도 있습니다. 그건 평가 데이터가 알려줄 것입니다.

## 진단을 위해 공유해 주실 데이터

다음 중 하나만 있으면 정밀 진단 가능:

1. **`_workspace/eval/r3.jsonl` 내용 일부** (10~20건 정도, query / retrieved_doc_ids / relevant_doc_ids / recall@5 컬럼)
2. **Step 1 스크립트 실행 결과** (view 분포)
3. **골드셋 query 톤 샘플** (5~10건의 query 문자열)

특히 1번이 있으면 per-query breakdown 으로 R3 가 어디서 손해/이득 봤는지 즉시 분석 가능합니다.
