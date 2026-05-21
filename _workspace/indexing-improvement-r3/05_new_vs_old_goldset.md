# 새 골드셋 vs 이전 골드셋 — R3 측정에서 어느 쪽이 유효한가

## 결론을 먼저

> **R3 효과 측정에는 이전 골드셋이 우월하다.** 새 골드셋은 절대 성능 측정에는 쓸 수 있지만 R3 효과 비교에는 부적합 — 두 가지 결정적 위험 때문에:
>
> 1. **데이터 누출 (data leakage)**: R3 인덱싱 후 골드셋을 LLM 으로 생성하면, R3 가상 질문과 골드셋 질의가 **동일/유사 LLM 으로 생성** 되어 인위적으로 가까워진다 → recall 점수가 R3 의 진짜 효과가 아니라 "같은 LLM 의 표현 일관성" 을 측정하게 됨.
> 2. **비교 baseline 부재**: 새 골드셋은 R3 적용 후 만들어진다 → 이전 시스템(origin/main) 에 대한 baseline 측정이 불가능 (다른 골드셋끼리는 메트릭 직접 비교 의미 없음).

## 새 골드셋의 의미가 있는 경우 / 없는 경우

| 시나리오 | 새 골드셋 | 이전 골드셋 |
|---------|---------|------------|
| R3 효과 측정 (Before/After 비교) | ❌ 부적합 (leakage + baseline 없음) | ✅ **권장** |
| R3 의 절대 성능 측정 (어떤 시스템인지) | △ 가능 (leakage 가능성) | ✅ |
| R4 (다음 라운드) 와 비교 baseline 확보 | ✅ (R3 후 베이스 측정) | ✅ |
| 사용자 표현 분포 변화 추적 (실제 채팅 로그 기반) | ✅ **유일한 방법** | ❌ |
| 골드셋 빌더 자체 검증 | ✅ | △ |

→ **R3 효과를 보고 싶다면 이전 골드셋 사용**. 새 골드셋은 별개 용도.

## 이전 골드셋이 여전히 의미있는지 — 5가지 체크포인트

골드셋의 ground truth 가 R3 변경 전후로 유지되는지 점검한다.

### ✅ 체크 1 — 정답 단위가 R3 변경에 무관한가

`GoldItem.relevant_doc_ids` (문서 ID) 와 `relevant_graph_entities` (이름·타입) 이 채점 키. **둘 다 인덱싱 정책 변화에 영향 없음**:

| 골드셋 필드 | R3 영향 |
|------------|---------|
| `relevant_doc_ids: list[int]` | ✅ 무영향 — 문서 ID 는 `meta_store.create_document`/`update_document_content` 가 안정적으로 유지 |
| `relevant_graph_entities: list[GraphEntityRef]` | ✅ 무영향 — 그래프 채널은 R3 가 건드리지 않음 (entity name/type/aliases/embedding 그대로) |
| `relevant_graph_relations: list[GraphRelationRef]` | ✅ 무영향 |
| `source_text_anchor: str` (judge 용) | ⚠️ 영향 — 청크 본문이 커지면 prefix 매칭 fail |
| `source_chunk_id: str` (deprecated) | ⚠️ 영향 — R3 재인덱싱 시 chunk_id 가 새로 발급. 그러나 deprecated 이므로 사용 안 함 |

**점검 방법**:
```bash
# 골드셋의 정답 doc_id 가 현재 meta_store 에 모두 존재하는지 확인
python -c "
import asyncio, yaml
from pathlib import Path
from context_loop.storage.metadata_store import MetadataStore

async def main():
    gold = yaml.safe_load(open('tests/eval/gold_set.yaml'))
    relevant = {d for item in gold['items'] for d in item.get('relevant_doc_ids', [])}
    store = MetadataStore(Path('~/.context-loop/data/metadata.db').expanduser())
    await store.initialize()
    existing = {d['id'] for d in await store.list_documents()}
    missing = relevant - existing
    print(f'골드셋 정답 doc {len(relevant)}건, 누락 {len(missing)}건')
    if missing: print(f'  누락 ID: {sorted(missing)[:10]}')
    await store.close()
asyncio.run(main())
"
```

→ 누락이 0건이면 doc-level 정답 그대로 유효.

### ✅ 체크 2 — 원본 문서가 골드셋 생성 후 변경되지 않았는가

골드셋이 생성된 시점 이후 Confluence 동기화나 파일 재업로드로 원본 본문이 바뀌었다면 정답의 의미가 달라질 수 있다 — **이건 R3 와 무관, 시간 흐름에 따른 자연 회귀**.

**점검 방법**:
```bash
# 골드셋 생성 시점(메타데이터) vs 현재 문서 updated_at 비교
python -c "
import yaml, asyncio
from pathlib import Path
from context_loop.storage.metadata_store import MetadataStore

async def main():
    gold = yaml.safe_load(open('tests/eval/gold_set.yaml'))
    created_at = gold.get('metadata', {}).get('created_at')
    print('골드셋 생성 시점:', created_at)
    store = MetadataStore(Path('~/.context-loop/data/metadata.db').expanduser())
    await store.initialize()
    relevant = {d for item in gold['items'] for d in item.get('relevant_doc_ids', [])}
    docs = [await store.get_document(d) for d in relevant]
    changed = [d for d in docs if d and str(d.get('updated_at', '')) > (created_at or '')]
    print(f'골드셋 이후 변경된 정답 문서 {len(changed)}/{len(relevant)}건')
    await store.close()
asyncio.run(main())
"
```

→ 변경 비율이 5% 미만이면 그대로 사용 OK. 20%+ 이면 변경된 항목만 골드셋에서 제외하거나 갱신 권고.

### ✅ 체크 3 — 데이터 누출이 없는가 (생성 시점)

이전 골드셋이 **R3 적용 전** 만들어졌다면 → R3 의 가상 질문 표현이 골드셋 질의에 누출될 수 없음 → **순수 평가 가능**.

만약 골드셋이 R3 적용 후 만들어졌다면 → 같은 LLM 이 생성한 가상 질문(R3) 과 골드 질의가 의미 공간에서 가까워져 R3 의 진짜 효과를 측정 못 함.

**점검 방법**:
```bash
git log --all --oneline -- tests/eval/gold_set.yaml | head -5
# 골드셋 마지막 변경 commit 이 R3 (PR #61 머지) 이전인지 확인
```

→ 골드셋 변경 시점 < R3 머지 시점 이면 누출 없음.

### ⚠️ 체크 4 — 측정하려는 메트릭이 R3 변경에 직접 결합되는가

| 메트릭 | R3 결합 | 결론 |
|--------|--------|------|
| `recall@k`, `precision@k`, `hit@k`, `nDCG@k`, `MRR` | doc_id 기반 | ✅ 그대로 유효 |
| `graph_recall@k` 등 | entity name/type | ✅ 그대로 유효 |
| Judge `overlap`, `entailment` | source_text_anchor → 청크 본문 prefix 매칭 | ⚠️ R3 후 80~90% 항목에서 fallback 경로 → 표본 ↓ → 신뢰성 ↓ |

→ **메인 metric 만 보면 그대로 유효**. Judge 가 결정 요인이면 anchor 매칭 보완 (1줄 fix) 또는 비활성화 필요.

### ✅ 체크 5 — 골드셋 표본 크기가 충분한가

R3 의 효과는 정의/방법 질의에서 **대폭** 향상 예상이지만, 키워드 질의에서는 미미할 수 있음. 골드셋이 모드별로 충분히 다양해야 평균 + 분포 모두 측정 가능.

**점검 방법**:
```bash
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml --dry-run --print-stats
# 또는 골드셋을 yaml 로 직접 봐서 difficulty / mode 분포 확인
```

→ 모드별 최소 10~20건씩 있으면 정량 비교 가능. 표본이 너무 적으면 R3 효과가 noise 에 묻힘.

## 권장 측정 절차

### Phase 1 — 이전 골드셋 검증 (5분)

1. 체크 1: 정답 doc_id 누락 확인
2. 체크 2: 골드셋 생성 후 문서 변경 비율
3. 체크 3: 골드셋 생성 시점이 R3 적용 전
4. 체크 5: 표본 분포

→ 모두 ✅ 면 그대로 사용. 일부 ⚠️ 면 영향 평가.

### Phase 2 — A/B 비교 (이전 골드셋 기반)

```bash
# Baseline (origin/main, R3 적용 전 인덱스 상태)
git checkout origin/main
# 인덱스가 origin/main 상태인지 확인 — 필요시 재인덱싱
python scripts/eval_search.py \
    --gold-set tests/eval/gold_set.yaml \
    --top-k 5 --no-judge \
    --output _workspace/eval/baseline.jsonl

# R3 적용 + 재인덱싱
git checkout claude/r3-multi-vector-doc-indexing
# 모든 confluence 문서 재인덱싱 — 가상 질문 생성 + 임베딩
# (해당 entrypoint 호출)
python scripts/eval_search.py \
    --gold-set tests/eval/gold_set.yaml \
    --top-k 5 --no-judge \
    --output _workspace/eval/r3.jsonl

# 비교 (recall@5, MRR, nDCG 등의 Δ)
python scripts/compare_runs.py \
    _workspace/eval/baseline.jsonl _workspace/eval/r3.jsonl
```

기대 결과 (R3 가상 질문 인덱싱이 의도대로 동작하면):
- `recall@5`: +5~15%p (정의/방법 질의 위주)
- `MRR`: +0.05~0.15 (첫 정답이 더 빨리 등장)
- `nDCG@5`: +0.05~0.10
- `precision@5`: 약간 ↑ 또는 무변화

### Phase 3 — 통계적 유의성 검증

`aggregate_with_variance` 로 N개 골드셋 / 재실행 차원의 variance 측정. R3 의 평균 향상 폭이 variance 보다 충분히 크면 유의미.

### Phase 4 — 신뢰 보강 (선택)

기존 골드셋 결과가 의심스럽거나 표본이 작으면, **R3 적용 전에 추가 사람-검증 골드셋** 을 한 번 더 만들어 비교. R3 이후 생성한 골드셋은 절대 사용하지 않는다 (leakage).

## "새 골드셋을 만들어 R3 와 비교" 가 안 되는 이유 — 다른 각도

같은 시스템을 두 골드셋에서 측정하면 메트릭 차이가 발생한다 (질의 분포, 난이도, 표본 차이). 즉:

```
score(SYSTEM, GOLD_SET_OLD)   ≠ score(SYSTEM, GOLD_SET_NEW)
score(BASELINE, GOLD_SET_OLD) ≠ score(R3,       GOLD_SET_NEW)
```

이걸 빼면 **시스템 차이 + 골드셋 차이가 섞여서** R3 의 진짜 효과를 분리 못 한다. 비교의 통계적 의미가 사라진다.

이전 골드셋으로 두 시스템을 측정해야:
```
Δ = score(R3, GOLD_SET_OLD) - score(BASELINE, GOLD_SET_OLD)
```
가 R3 자체의 효과를 깨끗하게 분리한다.

## 단 한 가지 예외 — "새 골드셋이 R3 비교에도 의미 있는" 시나리오

이전 골드셋이 망가졌거나(예: 정답 doc 의 대부분이 삭제됨) 너무 작거나 편향됐을 때만 **양쪽 시스템을 새 골드셋에서 다시 측정**. 이때 핵심:

- 새 골드셋은 **R3 의 가상 질문을 인지하지 못하는 도구로 생성** (사람 작성, 또는 R3 와 다른 LLM 으로 생성, 또는 R3 인덱스를 미적용한 본문에서 LLM 으로 생성).
- 두 시스템을 새 골드셋에서 측정 — Δ 가 R3 효과.

이 시나리오에서도 핵심은 **인덱싱 시스템과 골드셋 생성 시스템 분리** — 같은 LLM 이 둘 다 만들면 leakage.

## 한 줄 답

> **R3 효과 측정에는 이전 골드셋이 가장 신뢰성이 높다. 5가지 체크포인트(정답 단위, 원본 안정성, 누출 없음, 메트릭 결합, 표본) 가 모두 ✅ 면 그대로 쓰면 된다. 새 골드셋은 R4 와의 비교 baseline, 사용자 표현 분포 추적, 골드셋 빌더 검증 등 다른 용도로만 의미가 있다.**
