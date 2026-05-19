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
    --label diag --concurrency 4 --limit 30
```

**`eval/runs/diag.summary.json` 에서 점검:**

| 키 | 임계 | 의미 |
|---|---|---|
| `source_fetch_method_counts.fallback_first_chunk / total` | < 5% | 청크 분할 stable, anchor 그대로 유효 |
| | 5~20% | 청크 분할 변경 있음 — Judge overlap/entailment 모드 신뢰 저하. reference-free 권장 |
| | > 20% | anchor 대부분 stale — 그래프는 살려도 chunk 골드셋만으로 운영 |
| `graph_t4_disabled` (bool) | false | 그래프 T4 임베딩 매칭 정상 |
| | true | 임베딩 모델 호환 깨짐 — 그래프 메트릭만 재빌드 필요 |
| `judge_score_parse_failures / n_queries` | < 5% | Judge 응답 형식 안정 |
| `failure_rate` | < 5% | 평가 자체 안정 |

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
