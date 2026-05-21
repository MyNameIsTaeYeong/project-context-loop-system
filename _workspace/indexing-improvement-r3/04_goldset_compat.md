# R3 측정에 기존 골드셋 사용 가능성 검토

## 결론을 먼저

> **메인 검색 metric (recall/precision/MRR/nDCG/hit, graph_*) 은 그대로 사용 가능. judge 채점 (faithfulness/groundedness) 은 anchor 매칭 회귀로 신뢰성 저하 — 골드셋 재생성 또는 anchor 매칭 로직 보완이 필요.**

| 평가 영역 | R3 변경 후 사용 가능? | 사유 |
|----------|---------------------|------|
| `recall@k`, `precision@k`, `MRR`, `nDCG`, `hit@k` | ✅ **그대로 사용** | 골드셋이 이미 `relevant_doc_ids` (문서 ID) 단위로 채점. R3의 `document_id` dedup 정책과 정확히 정합 |
| `graph_recall@k`, `graph_precision@k`, etc. | ✅ **그대로 사용** | 그래프 채점은 entity name/type 기반 4-tier cascade. R3 그래프 채널 변경 없음 |
| Judge 채점 (overlap / entailment) | ⚠️ **신뢰성 저하** | `source_text_anchor` prefix 매칭이 청크 크기 변화로 80~90% 항목에서 fallback 경로로 빠짐 |
| Source 추적 (anchor / chunk_id) | ⚠️ **anchor 부분 fail** | 옛 청크 시작 200자가 anchor — R3 새 청크 시작과 불일치할 수 있음 |

## 코드 근거

### 메인 metric — doc_id 기반 (영향 없음)

`scripts/eval_search.py:385-413`:
```python
sorted_sources = sorted(assembled.sources, key=lambda s: (-(s.similarity or 0.0), s.document_id))
retrieved_doc_ids = [s.document_id for s in sorted_sources]
relevant = set(item.relevant_doc_ids)
row = {
    f"recall@{top_k}": recall_at_k(retrieved_doc_ids, relevant, top_k),
    f"precision@{top_k}": precision_at_k(retrieved_doc_ids, relevant, top_k),
    f"hit@{top_k}": int(hit_at_k(retrieved_doc_ids, relevant, top_k)),
    f"ndcg@{top_k}": ndcg_at_k(retrieved_doc_ids, relevant, top_k),
    "mrr": mrr(retrieved_doc_ids, relevant),
    ...
}
```

- `assembled.sources` 는 R3 변경 후 **document_id 단위로 dedup된 결과**.
- 골드셋 `relevant_doc_ids` 도 문서 ID.
- → **정확히 정합**. R3 변경 효과가 메트릭에 깨끗하게 반영된다.

### Anchor 매칭 — chunk 시작 200자 prefix 의존 (회귀 위험)

`src/context_loop/eval/synth.py:230-239` (anchor 생성):
```python
def make_text_anchor(content: str, *, max_chars: int = ANCHOR_MAX_CHARS) -> str:
    normalized = _normalize_whitespace(content)
    return normalized[:max_chars] if len(normalized) > max_chars else normalized
```

→ 골드셋 빌드 시 **(생성 당시) 청크 본문의 첫 200자** 가 anchor 로 박힘.

`scripts/eval_search.py:589-594` (anchor 매칭):
```python
if item.source_text_anchor:
    normalized_anchor = _normalize_for_anchor(item.source_text_anchor)
    for c in chunks:
        content = c.get("content") or ""
        if _normalize_for_anchor(content).startswith(normalized_anchor):
            return content, "anchor"
```

→ 현재 인덱싱된 청크들 중 **`.startswith(anchor)`** 인 청크를 찾음.

**R3 변경 후 anchor 매칭 시나리오**:

| 옛 골드셋 anchor 가 추출된 위치 | R3 인덱싱 후 결과 |
|--------------------------------|------------------|
| 옛 청크 1번 (문서 시작 부분) | R3 의 첫 청크 시작 = 문서/섹션 시작 → **prefix 매칭 OK** |
| 옛 청크 N>1 (문서 중간) | R3 1청크 문서: 청크 시작이 문서 시작 ≠ 중간 anchor → **fail** |
| 옛 청크 N>1 (큰 문서 섹션 후반부 chunk) | R3 섹션 폴백: 섹션 시작 ≠ 옛 청크 중간 시작 → **fail** |

평균적으로 옛 골드셋의 청크당 **10~20%만이 청크 1번** → **80~90% 항목에서 anchor 매칭 fail**.

`_fetch_source_text` 가 fail 시 `fallback_first_chunk` 또는 `fallback_doc_first_chunk` 경로로 빠지고, 호출부는 fallback method 일 때 judge 채점에서 제외하도록 설계됨 (코드 docstring 명시).

→ judge 채점 대상 항목 수가 대폭 줄어 **judge metric 표본이 작아져 신뢰성 ↓**.

### `source_chunk_id` (deprecated)

`src/context_loop/eval/gold_set.py:181-183`:
```python
# DEPRECATED: 기존 YAML 로드 호환용. 신규 생성에서는 emit 되지 않으며
# 채점·정렬 키로 사용 금지. 본문 lookup 의 2순위 fallback 으로만 쓰인다.
source_chunk_id: str | None = None
```

→ R3 변경으로 모든 청크 ID 가 새로 생성되므로 옛 골드셋의 `source_chunk_id` 는 무효. 다만 deprecated 이고 anchor 가 1순위, doc 첫 청크가 fallback이므로 **실질 영향 없음**.

## R3 변경별 골드셋 호환 매트릭스

| R3 변경 항목 | 골드셋 필드 영향 | 평가 영향 |
|-------------|----------------|----------|
| 문서 단위 청킹 (작은 문서 = 1청크) | `source_text_anchor` 매칭률 ↓ | judge 채점 표본 ↓ |
| 섹션 폴백 (큰 문서) | `source_text_anchor` 매칭률 ↓ | judge 채점 표본 ↓ |
| 가상 질문 임베딩 추가 | 없음 — vector_store metadata 확장만 | recall/MRR 분자 ↑ 가능성 (긍정적 효과 측정 대상) |
| `document_id` 단위 dedup | 없음 — `relevant_doc_ids` 와 정합 | 메트릭 그대로 측정 |
| `mcp.context_max_tokens` 32K 상향 | 없음 | judge 채점 시 retrieved_context 길이만 영향 (그러나 R3 의 핵심 효과 대상이 아님) |

## 권장 평가 절차

**Phase 1 — 메인 metric 비교 (즉시 가능)**:

```bash
# 1. 기존 골드셋 그대로 사용
# 2. baseline: origin/main (PR #61 머지 전)
# 3. R3 적용: 인덱스 재구축 (delete & recreate) 후 동일 골드셋으로 평가
python scripts/eval_search.py \
    --gold-set tests/eval/gold_set.yaml \
    --top-k 5 \
    --no-judge   # judge 비활성화 (anchor 회귀 회피)
```

→ recall/precision/MRR/nDCG/hit + graph_* metric 으로 R3 효과 측정. **여기서 가상 질문 인덱싱의 핵심 효과 (정의/방법 질의 정밀도 향상) 가 가장 잘 드러난다.**

**Phase 2 — judge 채점 (선택, 결정 필요)**:

옵션 a) **anchor 매칭 로직 보완** — prefix → substring 검색으로 완화:
```python
if _normalize_for_anchor(content).find(normalized_anchor) >= 0:
    return content, "anchor"
```
1줄 변경으로 R3 큰 청크에서도 anchor 가 발견되면 매칭. side-effect: 같은 anchor 가 여러 청크에 substring 으로 있으면 첫 번째 매칭 — 보통 동일 본문이라 무해.

옵션 b) **골드셋 재생성** — R3 인덱싱 후 `build_synthetic_gold_set.py` 재실행하여 새 청크 본문으로 anchor 갱신. 가장 깔끔하지만 비용 큼 (LLM 호출 등).

옵션 c) **judge 채점 비활성화** — `--no-judge` 로 메인 metric 만 측정. R3 효과의 90% 이상이 메인 metric 에 드러나므로 합리적 절충.

**권고**: 옵션 (c) → 옵션 (a) 순서. judge 가 R3 의 핵심 측정 목표가 아니므로 일단 비활성화하고, 필요 시 1줄 fix.

## 그래프 채점 영향 (별도)

R3 가 그래프 채널을 변경하지 않으므로 `relevant_graph_entities` / `relevant_graph_relations` 모두 그대로 유효. 다만 R2 가 머지되면 LLM 본문 그래프 추출이 문서 단위 1회 호출이 되어 entity 통합 품질이 ↑ → graph metric 도 R2 효과를 함께 측정하게 됨. 비교 시 어느 PR 의 변경인지 commit 단위로 구분 필요.

## 비교 실행 가이드 (Before/After)

```bash
# Baseline (origin/main, R3 적용 전)
git checkout origin/main
# 인덱스 재구축 (선택, gold 가 origin/main 상태로 만들어졌다면 skip)
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml --top-k 5 \
    --no-judge --output _workspace/eval/baseline.jsonl

# R3 적용
git checkout claude/r3-multi-vector-doc-indexing
# 모든 confluence 문서 재인덱싱 (가상 질문 임베딩 등록)
python scripts/reindex_all.py  # 또는 동등한 진입점
python scripts/eval_search.py --gold-set tests/eval/gold_set.yaml --top-k 5 \
    --no-judge --output _workspace/eval/r3.jsonl

# 비교
python scripts/compare_runs.py _workspace/eval/baseline.jsonl _workspace/eval/r3.jsonl
```

## 최종 답

> **기존 골드셋을 그대로 사용해도 됩니다 — 단, judge 채점은 anchor 매칭 회귀로 신뢰성이 떨어지므로 `--no-judge` 로 메인 metric 만 사용하세요. R3 의 핵심 효과인 "가상 질문 임베딩으로 검색 정밀도 향상" 은 recall/MRR/nDCG 로 충분히 측정됩니다.**

후속 작업 권고 (별도):
1. **`_fetch_source_text` 의 anchor 매칭을 prefix → substring 으로 완화** (1줄 변경, judge 채점 회복)
2. 옵션 — R3 인덱싱 후 골드셋 재생성으로 anchor 갱신 (가장 깔끔하지만 비용 큼)
