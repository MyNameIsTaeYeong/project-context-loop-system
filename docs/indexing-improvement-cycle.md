# 인덱싱 개선 사이클 운영 가이드

검색·RAG 시스템의 인덱싱 로직을 개선할 때, **무엇을 바꿨고 진짜 개선됐는지** 측정하기 위한 표준 절차.

## 핵심 원칙

1. **변경 전 baseline 결과를 반드시 보존** — 비교 기준점이 없으면 개선 판정 불가능
2. **같은 골드셋으로 paired 비교** — 골드셋이 baseline 에 fit 됐어도 paired diff 의 noise 는 양쪽에 동등하게 작용
3. **변경 유형에 따라 골드셋 유효성이 다름** — 전면 재빌드는 마지막 수단
4. **N≥5 다중 골드셋으로 변동성 baseline 확보** — 진짜 개선 vs 노이즈 구분

## 변경 유형별 골드셋 유효성

| 변경 유형 | doc_id | chunk 메트릭 | 그래프 메트릭 | 권장 |
|---|---|---|---|---|
| chunk_size / overlap 변경 | 유지 | ✅ 유효 | ✅ 유효 | paired 비교 그대로 |
| **content_hash 깨서 재인덱싱** (in-place UPDATE) | **유지** | **✅ 유효** | ⚠️ 부분 (description 재생성) | paired 비교 + 그래프만 부분 재빌드 |
| 임베딩 모델 변경 | 유지 | ✅ 유효 | ❌ T4 무효 | 그래프 description_embedding 재계산 |
| 그래프 추출 LLM 변경 | 유지 | ✅ 유효 | ⚠️ 부분 | 그래프 골드만 재빌드 |
| documents 테이블 truncate / DB 새로 만듦 | 변경 | ❌ 무효 | ❌ 무효 | 골드셋 전면 재빌드 |

`metadata_store.py:287-292` 의 in-place UPDATE 코드 확인: content_hash 가 깨져도 `documents.id` 는 유지. 골드셋의 `relevant_doc_ids` 가 살아 있어 chunk 메트릭 그대로 사용 가능.

## content_hash 를 깨는 방법

### 메커니즘 — 재처리 발동 흐름

```
인입 단계 (uploader/confluence/editor/git_repository)
   │ 새 콘텐츠 도착
   ▼
compute_content_hash(new_content)
   │
   ▼
DB 의 기존 content_hash 와 비교
   │
   ├─ 같음 → skip
   └─ 다름 → original_content + content_hash 갱신 + status = "changed"
                                                       │
                                                       ▼
                                    POST /api/documents/{id}/process 호출
                                    또는 process_document() 직접 호출
                                                       │
                                                       ▼
                                    1. 기존 청크/그래프 노드/엣지 삭제
                                    2. 재처리 파이프라인 실행
                                    3. status = "completed"
```

**핵심:** content_hash 만 SQL 로 깨도 자동 재처리되지 않습니다. 다음 둘 중 하나 필요:
1. 인입 트리거 (Confluence 동기화, 파일 재업로드, 에디터 저장 등) — content_hash 비교 후 자동 마킹·재처리
2. API 강제 호출 — content_hash 무관하게 즉시 재처리

### 방법 A — SQL 로 마킹만 (다음 인입 시 자동 재처리)

```sql
-- 특정 문서의 content_hash 를 깨서 다음 동기화 시 재처리 발동
UPDATE documents
   SET content_hash = 'broken',
       status = 'changed',
       updated_at = CURRENT_TIMESTAMP
 WHERE id = ?;

-- 특정 source_type 전체
UPDATE documents
   SET content_hash = 'broken',
       status = 'changed',
       updated_at = CURRENT_TIMESTAMP
 WHERE source_type = 'confluence_mcp';

-- 모든 문서
UPDATE documents
   SET content_hash = 'broken',
       status = 'changed',
       updated_at = CURRENT_TIMESTAMP;
```

**용도**: UI 의 "변경 감지됨" 상태 표시, 다음 Confluence 동기화 주기에 자동 재처리 발동. **단순 SQL 만으로는 재처리 즉시 시작 안 됨**.

### 방법 B — REST API 로 즉시 재처리 (권장)

```bash
# 특정 문서 즉시 재처리
curl -X POST http://127.0.0.1:8000/api/documents/{document_id}/process

# 여러 문서 (셸 스크립트)
for id in $(sqlite3 ~/.context-loop/data/metadata.db \
    "SELECT id FROM documents WHERE source_type='confluence_mcp'"); do
  curl -s -X POST "http://127.0.0.1:8000/api/documents/${id}/process"
  sleep 0.5  # 백그라운드 task 큐 보호
done
```

API 호출 시:
1. `meta_store.update_document_status(id, "processing")`
2. BackgroundTasks 로 `process_document()` 실행 (`web/api/documents.py:340`)
3. 청크·그래프 재생성 → `status = "completed"`

content_hash 무관하게 강제 실행되므로 가장 명확.

### 방법 C — SQL + API 조합 (변경 추적 + 즉시 재처리)

```bash
# 1. SQL 로 마킹 (감사 추적용)
sqlite3 ~/.context-loop/data/metadata.db <<EOF
UPDATE documents
   SET content_hash = 'broken-by-improvement-cycle-$(date +%Y%m%d)',
       status = 'changed'
 WHERE source_type = 'confluence_mcp';
EOF

# 2. 대상 문서 ID 추출
DOC_IDS=$(sqlite3 ~/.context-loop/data/metadata.db \
    "SELECT id FROM documents WHERE status='changed'")

# 3. API 로 일괄 재처리
for id in $DOC_IDS; do
  curl -s -X POST "http://127.0.0.1:8000/api/documents/${id}/process"
  sleep 0.5
done

# 4. 진행 모니터링
sqlite3 ~/.context-loop/data/metadata.db \
  "SELECT status, COUNT(*) FROM documents GROUP BY status;"
```

## 표준 인덱싱 개선 사이클

### 0. 사전 준비 (한 번만)

```bash
# 변동성 baseline 확보용 다중 골드셋 빌드 (N=5)
python scripts/build_synthetic_gold_set.py \
    --generator-endpoint http://strong-model:8080/v1 --generator-model gpt-4o \
    --judge-endpoint http://other:8080/v1 --judge-model claude-haiku \
    --seed 42 --generator-seed-base 1000 \
    --n-gold-sets 5 --concurrency 4 \
    --include-graph-questions --score-relations \
    --output eval/gold_sets/run.yaml

# → eval/gold_sets/run_001.yaml ~ run_005.yaml
```

### 1. baseline 평가 (변경 전)

```bash
python scripts/eval_search.py \
    --gold-set-glob "eval/gold_sets/run_*.yaml" \
    --label baseline --concurrency 4 \
    --judge --judge-endpoint http://other:8080/v1 \
    --judge-model claude-haiku --judge-mode reference-free \
    --judge-n-samples 3

# 산출:
# - eval/runs/baseline_run_001.summary.json ~ _005.summary.json
# - eval/runs/baseline.aggregate.summary.json  ← mean/std/min/max
# - eval/runs/baseline_run_001.csv ~ _005.csv
```

**`baseline.aggregate.summary.json` 보존 — 모든 비교의 기준점.**

### 2. 인덱싱 로직 변경 (코드 수정)

예시:
- `processor/chunker.py` 의 chunk_size 변경
- `processor/embedder.py` 의 임베딩 모델 교체
- `processor/graph_extractor.py` 의 추출 프롬프트 개선

### 3. 영향 받는 문서 재인덱싱

위의 방법 B (REST API) 권장:

```bash
# 모든 문서 재처리
for id in $(sqlite3 ~/.context-loop/data/metadata.db "SELECT id FROM documents"); do
  curl -s -X POST "http://127.0.0.1:8000/api/documents/${id}/process"
  sleep 1
done

# 또는 source_type 별 (예: confluence_mcp 만)
for id in $(sqlite3 ~/.context-loop/data/metadata.db \
    "SELECT id FROM documents WHERE source_type='confluence_mcp'"); do
  curl -s -X POST "http://127.0.0.1:8000/api/documents/${id}/process"
  sleep 1
done

# 진행 상황 모니터링 (반복 실행)
sqlite3 ~/.context-loop/data/metadata.db \
  "SELECT status, COUNT(*) FROM documents GROUP BY status;"
```

### 4. 진단 1회 — 골드셋 stale 정도 확인

```bash
# 같은 골드셋으로 진단 실행 (전체 평가 전 빠른 점검)
python scripts/eval_search.py \
    --gold-set eval/gold_sets/run_001.yaml \
    --label diag --concurrency 4 --limit 50
```

`eval/runs/diag.summary.json` 의 4개 진단 키를 본다. 각 키의 정의·계산·임계 해석을 아래 자세히 설명한다.

#### 진단 키 1. `source_fetch_method_counts`

**정의**: 평가 시 골드 항목의 "정답 청크 본문" 을 회복한 경로별 카운트. `eval_search.py` 의 `_fetch_source_text` 가 어느 방식으로 source text 를 찾았는지 추적 (S0 P6 에서 도입).

**값 (dict)**:

```json
"source_fetch_method_counts": {
  "anchor": 145,
  "chunk_id": 0,
  "fallback_first_chunk": 8,
  "fallback_doc_first_chunk": 0,
  "empty": 2
}
```

| method | 의미 | 신뢰도 |
|---|---|---|
| `anchor` | 골드셋의 `source_text_anchor` (앞 200자) 가 새 청크의 본문 prefix 와 매칭 성공 | ✅ **최선** — 정확히 같은 청크 |
| `chunk_id` | (deprecated, 옛 골드셋 호환) `source_chunk_id` UUID 일치 | ✅ 정확 |
| `fallback_first_chunk` | anchor·chunk_id 매칭 실패 → 정답 문서의 **첫 청크** 채택 | ⚠️ **잘못된 청크 가능성** |
| `fallback_doc_first_chunk` | `source_document_id` 없음 → `relevant_doc_ids[0]` 의 첫 청크 | ⚠️ 더 약함 |
| `empty` | 청크가 0개 또는 문서 미발견 | ❌ Judge 채점 불가능 |

##### `fallback_first_chunk` 동작 예시

```
item (골드셋 한 항목)
  ├── source_document_id: 42
  ├── source_text_anchor: "신용카드 결제 시 일일 한도는 100만원..."  (앞 200자)
  └── source_chunk_id: ""  (deprecated, 새 골드셋은 비어 있음)
        │
        ▼
  store.get_chunks_by_document(42)  ── 문서 42 의 새 청크 목록
        │
        ▼
  ┌─ 1차: anchor prefix 매칭 ────────────────────────┐
  │   normalize(chunk.content).startswith(           │
  │     normalize(anchor))  ?                        │
  │   → 성공: method="anchor"                        │
  └──────────────────────────────────────────────────┘
        │ 매칭 실패
        ▼
  ┌─ 2차: source_chunk_id UUID 매칭 (옛 골드셋용) ──┐
  │   → 성공: method="chunk_id"                     │
  └──────────────────────────────────────────────────┘
        │ 매칭 실패 또는 chunk_id 비어 있음
        ▼
  ┌─ 3차: FALLBACK — 문서의 첫 청크 ────────────────┐
  │   return (chunks[0].content,                    │
  │           "fallback_first_chunk")  ◄── ★ 발동 ★ │
  └──────────────────────────────────────────────────┘
```

**가장 흔한 발동 시나리오 — chunk_size 변경:**

Baseline 빌드 (`chunk_size=512`) — 문서 "결제 가이드" (doc_id=42) 가 5 청크로 분할:

| chunk_index | 본문 prefix |
|---|---|
| 0 | `"결제 시스템 개요\n\n본 가이드는 결제 처리..."` |
| **1** | **`"신용카드 결제 시 일일 한도는 100만원이고 월간 한도는 1000만원입니다..."`** ★ |
| 2 | `"환불 정책은 결제 후 30일..."` |

골드셋이 chunk[1] 선택, `source_text_anchor` = 위 본문 앞 200자.

Treatment 시점 (`chunk_size=1024` 로 변경 + 재인덱싱) — 같은 문서가 3 청크로 새로 분할:

| chunk_index | 본문 prefix |
|---|---|
| 0 | `"결제 시스템 개요\n\n본 가이드는...신용카드 결제 시 일일 한도는 100만원이고..."` (옛 0+1 통합) |
| 1 | `"환불 정책..."` |
| 2 | `"부정 거래..."` |

평가 시 anchor `"신용카드 결제 시 일일 한도는..."` 로 시작하는 청크 찾기:
- new_chunk[0] prefix `"결제 시스템 개요..."` → 미스
- new_chunk[1] prefix `"환불 정책..."` → 미스
- new_chunk[2] prefix `"부정 거래..."` → 미스
- → **fallback: new_chunk[0]** 반환, `method="fallback_first_chunk"`

진짜 정답 정보 ("일일 한도 100만원") 가 new_chunk[0] 의 중간에 묻혀 있지만 prefix 가 달라 anchor 매칭 실패. Judge 가 받는 source text 는 `"결제 시스템 개요..."` (잘못된 청크).

**메트릭별 영향:**

| 메트릭 | fallback 시 영향 |
|---|---|
| `recall@k`, `precision@k`, `hit@k`, `ndcg@k`, `mrr` | **영향 없음** — `retrieved_doc_ids` 와 `relevant_doc_ids` (doc_id 기반) 만 비교. `_fetch_source_text` 는 Judge 용 source 회복일 뿐 |
| `graph_*` | **영향 없음** — entity_name/type 기반 |
| `judge_score` (`overlap` / `entailment` 모드) | **표본 손실** — fallback 시 `judge_score = None` 으로 자동 skip + `judge_skip_reason = "source_fallback"` (S0 P6) |
| `judge_score` (`reference-free` 모드) | **영향 없음** — source 안 봄 (S2 N-M2 로 fallback 무시) |

**임계 해석** (`fallback_first_chunk + fallback_doc_first_chunk / total`):

| 비율 | 발생 원인 추정 | 운영 결정 |
|---|---|---|
| **< 5%** | 청크 분할 stable. 일부 짧은 청크나 normalize edge case 만 fallback | 그대로 사용. `overlap`/`entailment` Judge 도 사용 가능 |
| **5~20%** | chunk_size·overlap·청킹 알고리즘 변경 의심. 일부 anchor 가 새 청크 prefix 와 불일치 | `--judge-mode reference-free` 강제. chunk 메트릭은 그대로 유효 |
| **> 20%** | 대규모 청크 재분할 또는 normalize 비대칭 | reference-free 만 사용. anchor 자체가 stale 이라 부분 재구성 고려 ([S4-P2](./eval-system-followup.md)) |

#### 진단 키 2. `graph_t4_disabled` (+ `graph_t4_skip_count`)

**정의**: 그래프 4-tier cascade matching 의 **T4 (embedding cosine)** 단계가 한 번이라도 skip 됐는지. `build_embed_fn` (`graph_match.py:182-199`) 의 async 호환 분기가 None 반환 시 발동.

**값**:
- `graph_t4_disabled: true | false` — 평가 실행 중 한 번이라도 T4 skip 발생
- `graph_t4_skip_count: int` — 총 skip 횟수

**의미**: T4 가 skip 되면 4-tier cascade 가 T1 (exact name+type) / T2 (alias) / T3 (normalize) 만 시도. **T4 가 흡수해야 할 case (description 만 비슷한 의미 매칭, type-drift 등) 를 못 잡음** → `graph_recall@k` 하락.

**임계 해석:**

| 상태 | 의미 | 원인 |
|---|---|---|
| `false` | T4 정상 작동 | 임베딩 클라이언트 정상 |
| `true` + skip_count < 5% | 일부 항목만 skip — 비동기 contention 등 일시 이슈 | 재실행 또는 무시 가능 |
| `true` + skip_count ≥ 5% | T4 사실상 미작동 | **임베딩 모델 변경**으로 골드셋 `description_embedding` (옛 vector) 과 호환 깨짐 |

**진단 명령** — 골드셋의 임베딩 모델과 현재 config 비교:

```bash
python -c "
import yaml
gs = yaml.safe_load(open('eval/gold_set.yaml'))
print('골드셋 embedding_model:', gs['metadata'].get('embedding_model'))
"
grep -E "^processor:" -A 5 ~/.context-loop/config.yaml
# → 두 값이 다르면 임베딩 모델 변경, 그래프 description_embedding 재계산 또는 그래프 골드셋 재빌드
```

#### 진단 키 3. `judge_score_parse_failures` (+ `judge_score_success_count`)

**정의**: Judge LLM 이 0~5 점수를 JSON 으로 응답해야 하는데 파싱 실패한 횟수 (S0 P5 에서 도입). `judge_answer` 가 `-1` 반환 시 `judge_score = None` 으로 분리.

**값**:

```json
"judge_score_parse_failures": 3,
"judge_score_success_count": 147
```

**임계 해석** (`judge_score_parse_failures / n_queries`):

| 비율 | 의미 | 운영 결정 |
|---|---|---|
| **< 5%** | Judge 응답 형식 안정 | 평균 `judge_score` 신뢰 가능 |
| **5~10%** | Judge 모델이 가끔 JSON 형식 깸 | `reasoning_mode` 점검 (reasoning 토큰이 응답 본문에 섞이는지). 다른 Judge 모델 시도 |
| **> 10%** | Judge 응답이 빈번히 깨짐 | **Judge 모델 교체 또는 프롬프트 점검**. 평균 `judge_score` 가 손실된 표본으로 계산돼 편향 가능 |

**흔한 원인:**
- Judge 가 JSON 외에 자연어 prefix 출력 (예: "응답: {"score": 4}")
- 추가 키 출력 또는 잘못된 형식
- Reasoning 토큰을 응답 본문에 섞음 (vLLM Qwen3 등)

#### 진단 키 4. `failure_rate` (+ `n_failed`, `n_successful`)

**정의**: 평가 자체가 exception 으로 실패한 질의 비율 (S0 P9 에서 도입). 검색 시스템이 timeout·import error 등으로 응답 못한 경우.

**값**:

```json
"n_queries": 150,
"n_failed": 3,
"n_successful": 147,
"failure_rate": 0.02
```

**계산**: `n_failed / n_queries`. 평균 메트릭 분모는 `n_successful` 기준 (실패는 자동 제외 — S0 P9 의 핵심 안전장치).

**임계 해석:**

| 비율 | 의미 | 운영 결정 |
|---|---|---|
| **< 5%** | 평가 안정 — 메트릭 분모가 거의 전체 | 그대로 사용 |
| **5~10%** | 가끔 timeout/error | per-question CSV 의 `error` 컬럼 확인. 재실행으로 우회 가능 |
| **> 10%** | 평가 환경 깨짐 | **재실행 전 환경 점검**. 메트릭 비교 의미 없음 |

**진단 명령** — 실패 질의 에러 메시지 확인:

```bash
python -c "
import csv
with open('eval/runs/diag.csv') as f:
    for row in csv.DictReader(f):
        if row.get('metric_failed') == 'True':
            print(row['id'], row.get('error', '')[:100])
"
```

흔한 원인:
- `assemble_context_with_sources` timeout (대용량 그래프 탐색)
- LLM endpoint 연결 끊김 (reranker / HyDE 호출)
- ChromaDB 락 충돌 (높은 `--concurrency` 일 때)

#### 진단 키 종합 표

| 키 | 안전 | 주의 | 위험 |
|---|---|---|---|
| `source_fetch_method_counts.fallback_*  / total` | <5% | 5~20% | >20% |
| `graph_t4_disabled` | `false` | `true` + skip <5% | `true` + skip ≥5% |
| `judge_score_parse_failures / n_queries` | <5% | 5~10% | >10% |
| `failure_rate` | <5% | 5~10% | >10% |

**운영 결정 흐름:**

```
모두 "안전" → 그대로 사용
─────────────────────────
한 항목 "주의" + 나머지 안전 → 그대로 사용하되 해당 영역 주의
─────────────────────────
한 항목 "위험" + 나머지 안전 → 해당 영역 우회
  - fallback 위험 → reference-free Judge 강제
  - T4 위험 → 그래프 골드셋 재빌드
  - parse 위험 → Judge 모델 교체
  - failure 위험 → 환경 점검
─────────────────────────
두 항목 이상 "위험" → 환경 점검 후 재실행. 메트릭 비교 의미 없음
```

#### 진단 자동 추출 명령

```bash
python -c "
import json, sys
s = json.load(open(sys.argv[1]))
n = s.get('n_queries', 0) or 1

print('=== 진단 요약 ===')
fmc = s.get('source_fetch_method_counts') or {}
fb = fmc.get('fallback_first_chunk', 0) + fmc.get('fallback_doc_first_chunk', 0)
fb_total = sum(fmc.values())
print(f'source fallback : {fb}/{fb_total} = {fb/max(fb_total,1):.1%}')
print(f'  method 분포   : {fmc}')

print(f'graph_t4        : disabled={s.get(\"graph_t4_disabled\", False)}, '
      f'skip_count={s.get(\"graph_t4_skip_count\", 0)}')

jpf = s.get('judge_score_parse_failures', 0)
print(f'judge parse fail: {jpf}/{n} = {jpf/n:.1%}')

print(f'failure_rate    : {s.get(\"failure_rate\", 0):.1%} '
      f'({s.get(\"n_failed\", 0)}/{n})')
" eval/runs/diag.summary.json
```

출력 예시:

```
=== 진단 요약 ===
source fallback : 8/50 = 16.0%
  method 분포   : {'anchor': 42, 'fallback_first_chunk': 8}
graph_t4        : disabled=False, skip_count=0
judge parse fail: 0/50 = 0.0%
failure_rate    : 0.0% (0/50)
```

→ 16% 면 5~20% 구간 (주의) — `--judge-mode reference-free` 권장, chunk 메트릭은 그대로 사용 가능.

### 5. 등급별 분기

#### 5-A. 진단이 모두 안전 (fallback < 5%, T4 정상)

```bash
# 전체 평가 그대로 진행
python scripts/eval_search.py \
    --gold-set-glob "eval/gold_sets/run_*.yaml" \
    --label treatment --concurrency 4 \
    --judge --judge-endpoint http://other:8080/v1 \
    --judge-model claude-haiku --judge-mode reference-free \
    --judge-n-samples 3
```

#### 5-B. fallback 5~20% 또는 그래프 T4 disabled

```bash
# 그래프 골드셋만 부분 재빌드 (chunk 골드셋은 보존)
python scripts/build_synthetic_gold_set.py \
    --include-graph-questions --score-relations \
    --seed 42 --generator-seed-base 1000 \
    --output eval/gold_sets/run_graph_new.yaml \
    [기존 옵션 동일]

# 평가 시 chunk 는 기존, graph 는 새 골드셋 사용
# (현 시스템 한계 — 단일 골드셋 평가만 지원. 두 평가를 따로 돌려야 함)
python scripts/eval_search.py --gold-set eval/gold_sets/run_001.yaml \
    --label treatment_chunk --no-graph
python scripts/eval_search.py --gold-set eval/gold_sets/run_graph_new.yaml \
    --label treatment_graph
```

#### 5-C. fallback > 20% (anchor 대부분 깨짐)

- chunk 골드셋도 anchor 가 stale — `overlap`/`entailment` Judge 모드는 신뢰 못 함
- **`reference-free` Judge 모드만 사용**, anchor 안 봄
- `judge_skip_count` 증가 가능 — `source_fetch_method_counts` 모니터링

### 6. paired 비교

```bash
python scripts/compare_runs.py \
    --baseline   eval/runs/baseline.aggregate.summary.json \
    --treatment  eval/runs/treatment.aggregate.summary.json \
    --baseline-csv  eval/runs/baseline_run_001.csv \
    --treatment-csv eval/runs/treatment_run_001.csv \
    --min-effect-size 0.02 \
    --out eval/runs/compare.json
```

### 7. 의사결정 — compare_runs 출력 해석

| 메트릭 결과 | 판정 |
|---|---|
| `p_imp ≥ 0.95` + `CI95 lo > 0` + `Cohen d ≥ 0.5` | ✅ **유의미한 개선, 머지 가능** |
| `p_imp ≥ 0.95` + `CI95 lo > 0` + `Cohen d ∈ [0.2, 0.5)` | ⚠️ 작은 효과 — fit 편향 가능성. 전략 8 (양방향 골드셋) 로 보강 |
| `p_imp ∈ [0.5, 0.95)` | ⚠️ 약한 신호 — 추가 표본 또는 다른 메트릭 확인 |
| `p_imp < 0.5` | ❌ 개선 없음 또는 회귀 |
| N < 10 (`*` 마킹) | ⚠️ 표본 부족 — 골드셋 N 증가 |
| `config_mismatch` 차단 | ❌ baseline/treatment 설정 다름 — 같은 옵션으로 재실행 |

### 8. 보강 — 양방향 골드셋 (불공정 우려 시)

baseline 골드셋이 변경 전 인덱스에 fit 되어 있어 treatment 에 불리할 우려가 있으면:

```bash
# treatment 인덱스에서도 골드셋 빌드
python scripts/build_synthetic_gold_set.py \
    --seed 1042 --generator-seed-base 2000 \
    --n-gold-sets 5 --output eval/gold_sets/run_treatment.yaml \
    [동일 옵션]

# baseline 인덱스를 일시 복원하거나 archive 에서 평가
# (인덱스를 동시 보관해야 가능 — DB 백업/복원 인프라 필요)

# 양방향 매트릭스 산출
# - eval(baseline-idx, gold_baseline)
# - eval(baseline-idx, gold_treatment)
# - eval(treatment-idx, gold_baseline)
# - eval(treatment-idx, gold_treatment)
```

진짜 개선이면 `eval(treatment, gold_baseline) ≥ eval(baseline, gold_baseline)` — treatment 가 baseline 의 골드셋도 잘 처리.

## 변경 이력 추적 — 빌드 메타데이터 활용

매 평가 시 골드셋 메타데이터를 함께 기록:

```bash
# 골드셋의 메타데이터 확인
python -c "
import yaml
with open('eval/gold_sets/run_001.yaml') as f:
    gs = yaml.safe_load(f)
meta = gs.get('metadata', {})
for k in ['generator_model', 'judge_model', 'embedding_model',
          'graph_match_threshold_default', 'self_evaluation_warning',
          'generator_temperature', 'generator_seed_base',
          'filter_applied']:
    print(f'{k}: {meta.get(k)}')
print(f'stats: {meta.get(\"stats\")}')
"
```

`eval_search.py` 의 summary 도 자동으로 `gold_set_sha256`, `gold_set_generator_model`, `judge_mode`, `judge_is_self`, `embedding_model`, `llm_model` 을 기록 (S0 P8). 사후 추적 가능.

## 트러블슈팅

| 증상 | 진단 | 대응 |
|---|---|---|
| `compare_runs` 가 `[CONFIG MISMATCH]` 차단 | baseline/treatment 의 EQUIVALENCE_KEYS 다름 | mismatch 키 확인 후 동일 옵션으로 재실행. judge_mode 차이가 흔한 원인 (S3 N-M1) |
| `failure_rate` > 10% | 검색 시스템이 timeout/exception 다발 | 인덱싱 후 vector store/graph store 로드 상태 확인. `--limit 30` 로 빠른 진단 |
| `judge_score` 평균이 이전 대비 크게 다름 | judge_mode 변경 또는 self-knowledge 발현 | summary 의 `judge_mode` 확인. reference-free 로 통일 권장 |
| `graph_recall@5` 가 0 근처 | graph_t4_disabled 또는 description 변경 | `graph_t4_disabled` 키 확인. true 면 임베딩 호환 깨짐 → 그래프 골드셋 재빌드 |
| 재처리 후에도 청크가 안 생김 | `process_document` 호출 안 됐거나 실패 | `processing_history` 테이블 확인. 실패면 logs 점검 |

```sql
-- 재처리 이력 점검
SELECT document_id, action, status, error_message, completed_at
  FROM processing_history
 ORDER BY id DESC LIMIT 20;

-- status 별 분포
SELECT status, COUNT(*) FROM documents GROUP BY status;

-- 청크가 없는 문서 (재처리 실패 의심)
SELECT d.id, d.title, d.status,
       (SELECT COUNT(*) FROM chunks WHERE document_id = d.id) AS chunk_count
  FROM documents d
 WHERE chunk_count = 0;
```

## 권장 보조 도구 — 향후 작업

현 시스템에는 다음 인프라가 없습니다. 본격적인 인덱싱 개선 사이클을 자주 돌리려면 후속 PR 로 보강 권장 ([`docs/eval-system-followup.md`](./eval-system-followup.md) 참조):

1. **anchor 골드셋** — 답 텍스트로 채점하여 인덱스 변경에 독립
2. **부분 재구성 도구** — 골드셋의 질문은 보존하면서 anchor·description_embedding 만 갱신
3. **인덱스 스냅샷** — baseline/treatment 인덱스 동시 보관해서 양방향 골드셋 평가
4. **카나리 질문 셋** — 운영 핵심 질문 hand-curated 셋 (회귀 검출 전용)

## 관련 문서

- 평가 스크립트 4종 사용법: [`docs/eval-scripts.md`](./eval-scripts.md)
- 환경 설정·실행: [`docs/setup.md`](./setup.md)
- 평가 시스템 미흡 사항 + 다음 세션 작업: [`docs/eval-system-followup.md`](./eval-system-followup.md)
- 평가 신뢰성 감사·패치 하네스: `.claude/skills/rag-eval-audit/SKILL.md`, `.claude/skills/rag-eval-fix/SKILL.md`
