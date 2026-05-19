# 평가 시스템 — 미흡 사항 및 다음 세션 작업 노트

PR #52~#55 시리즈로 신뢰도 **C → A** 회복 완료. 단 인덱싱 개선 사이클을 본격적으로 돌리기에는 다음 인프라가 부족하며, 다음 세션에서 이어서 작업한다.

## 현재 상태 요약 (2026-05-19 기준)

| 항목 | 상태 |
|---|---|
| 종합 신뢰도 등급 | **A** (v4 재감사, 메인 + 두 서브 감사관 합치) |
| 머지 대기 PR | #52, #53, #54, #55, #56 (sequential), 본 노트는 별도 |
| 신뢰도 진행 이력 | C (v1) → B+ (S0/S1 12건) → A− (S2 6건) → A (S3 9건) |
| 적용된 패치 총합 | 27건 (Critical 5 + High 10 + Medium/Low 12+) |
| 잔여 HIGH | 0건 |
| 잔여 Medium/Low | 의도된 트레이드오프 5건 (Anthropic seed 미지원·BCa 미지원·N≥10 권장 등) |

## 우선순위 — 다음 세션 작업 후보

### S4-P1. anchor 골드셋 (답 텍스트 기반 채점) — Critical

**문제**: 현 골드셋은 `relevant_doc_ids` (정수 ID) 와 `source_text_anchor` (200자 prefix) 로만 채점. 인덱스가 크게 바뀌면 (DB truncate, doc_id 재부여) 골드셋 전면 무효 → baseline 비교 기준점 손실.

**해결**:
- `GoldItem` 에 신규 필드 추가:
  - `expected_answer_keywords: list[str]` — 답에 포함되어야 할 키워드 (substring 검사)
  - `must_contain_facts: list[str]` — Judge entailment 로 의미 검증할 사실 단위
  - `must_not_contain: list[str]` — 부정 검증 (예: "오답 키워드 포함 시 감점")
- `eval_search.py` 에 `--judge-mode keyword-match` / `--judge-mode fact-entailment` 모드 추가
- `build_synthetic_gold_set.py` 의 Generator 프롬프트에 "답 키워드 3-5개 함께 출력" 지시
- Judge 가 `retrieved_context` 에서 답을 생성하고 keyword/fact 매칭으로 채점

**기대 효과**: 인덱싱 변경에 완전 독립적인 baseline. DB truncate 후에도 비교 기준점 유지.

**작업 범위**: GoldItem 스키마 + Generator 프롬프트 + Judge 모드 2종 + 평가 메트릭. **~5~7 파일, 약 800줄.**

**의존**: PR #52~#55 머지 완료.

---

### S4-P2. 부분 재구성 도구 — High

**문제**: 임베딩 모델 변경 또는 그래프 추출 LLM 변경 시 현재는 골드셋 전면 재빌드 외 옵션 없음. 그러나 골드셋의 질문(LLM 비용 + 통과한 4단계 게이트) 은 가치 있는 자산.

**해결**: 새 스크립트 `scripts/reconstruct_gold_set.py`:
```bash
python scripts/reconstruct_gold_set.py \
    --input eval/gold_set.yaml \
    --update-anchors            # source_text_anchor 만 새 청크에서 재추출
    --update-graph-embeddings   # description_embedding 만 새 모델로 재계산
    --update-aliases            # entity_aliases 만 새 LLM 으로 재생성
    --output eval/gold_set_reconstructed.yaml
```

질문 본문·정답 doc_id·정답 entity name 은 보존. 인덱스에 의존하는 부분만 갱신.

**작업 범위**: 신규 스크립트 + GoldItem 부분 수정 헬퍼. **~400줄.**

**의존**: S4-P1 (GoldItem 확장) 와 함께 작업하면 좋음.

---

### S4-P3. 카나리 질문 셋 — High

**문제**: 합성 골드셋은 다양성 좋지만 운영의 핵심 질문(매출 영향 큰 질문, 자주 묻는 질문) 을 직접 다루지 않음. 회귀 검출이 통계 평균에 묻힐 수 있음.

**해결**:
- `eval/canary_set.yaml` — hand-curated 10~30 질문
- 각 항목: (질문, 반드시 매칭돼야 하는 doc_id 또는 키워드, 회귀 임계)
- `eval_search.py --canary eval/canary_set.yaml` 옵션 — 카나리만 별도 채점
- compare_runs 에서 카나리 항목은 **별도 PASS/FAIL** 보고 (평균에 묻히지 않음)

**작업 범위**: 신규 YAML 스키마 + eval/compare 분기. **~300줄.**

---

### S4-P4. 인덱스 스냅샷 / 양방향 골드셋 평가 — High

**문제**: baseline 인덱스 위에서 만든 골드셋은 treatment 에 불리할 수 있음 (fit 편향). 양방향 평가가 가장 공정하지만 현재 baseline 인덱스를 동시 보관할 방법이 없음.

**해결**:
- `scripts/snapshot_index.py {snapshot_name}` — `metadata.db`, vector store, graph store 를 `snapshots/{name}/` 로 백업
- `scripts/eval_search.py --against-snapshot {name}` — 스냅샷 위에서 평가 (현재 인덱스 영향 없음)
- 양방향 평가:
  ```bash
  python scripts/snapshot_index.py baseline-2026-05-19
  # ... 인덱싱 변경 + 재처리 ...
  python scripts/eval_search.py --gold-set gold_baseline.yaml --label tr_on_gb
  python scripts/eval_search.py --gold-set gold_baseline.yaml --label bl_on_gb \
      --against-snapshot baseline-2026-05-19
  ```

**작업 범위**: 신규 스크립트 + eval_search 옵션 + 스냅샷 디렉터리 관리. **~500줄.** 가장 큰 작업.

**의존**: 없음. 단독 진행 가능.

---

### S4-P5. compare_runs BCa bootstrap — Medium

**문제**: 현 `paired_bootstrap` 은 단순 percentile bootstrap. 작은 N (< 20) 에서 CI 가 편향될 수 있음.

**해결**: BCa (bias-corrected and accelerated) bootstrap 추가:
- bias correction `z_0` = `Φ⁻¹(P(θ̂* < θ̂))`
- acceleration `a` = jackknife 추정 — `1/6 · Σ(θ̄ − θ_i)³ / (Σ(θ̄ − θ_i)²)^(3/2)`
- 보정된 quantile 로 CI 추출

**작업 범위**: `compare_runs.py:paired_bootstrap` 확장 + 단위 테스트. **~150줄.**

**의존**: 없음.

---

### S4-P6. AnthropicClient seed 우회 — Medium

**문제**: Anthropic SDK 가 seed 파라미터를 미지원. Generator/Judge 가 Anthropic 모델이면 결정성 보장 안 됨.

**해결**: `llm_client.AnthropicClient.complete` 에서 `n_samples` 옵션 시:
- 같은 입력을 N회 호출
- N개 응답 중 median (또는 가장 많은 응답) 채택
- 결정성을 confidence 로 대체

**작업 범위**: AnthropicClient 수정 + synth/eval 호출자 옵션 전파. **~200줄.**

**의존**: 없음. 우선순위 낮음 (운영이 주로 endpoint 사용).

---

### S4-P7. 시스템 RAG context_assembler tie-breaker 동기화 — Medium

**문제**: S2 P14 로 평가 측 (`eval_search.py:293-296`) 에 명시 stable sort 추가됐지만, 운영 RAG (`assemble_context_with_sources`) 자체의 정렬은 동기화 안 됨. 운영 응답과 평가 결과 간 미세 불일치 가능.

**해결**: `context_assembler.py` 의 source 정렬을 `(−similarity, document_id asc)` 로 명시. 운영 동작 변경이라 신중한 머지 필요.

**작업 범위**: `context_assembler.py` 수정 + 회귀 테스트. **~100줄.**

**의존**: 운영 영향 검토 필요 — 사용자 응답에 미세 변화 가능.

---

### S4-P8. Judge 모드별 분산 측정 자동 활성 — Low

**문제**: `--judge-n-samples 3` 은 수동 옵션. 작은 골드셋이나 첫 평가에서 자동으로 분산 측정하면 안전.

**해결**: `eval_search.py` 에 `--auto-variance` 옵션 — N < 20 이면 자동으로 n_samples=3.

**작업 범위**: CLI 옵션 + 자동 분기. **~30줄.** 작음.

---

### S4-P9. calibrate_graph_match LLM 합성 alias — Low

**문제**: `calibrate_graph_match.py:177` 의 alias-only 양성 쌍은 substring/prefix 4글자 휴리스틱. 진짜 의미 alias (다른 표기지만 같은 의미) 는 미커버.

**해결**: `--synth-alias` 옵션 — 양성 쌍 부족 시 LLM 으로 합성 alias 생성 ("Pay Service" → "결제 서비스", "PaymentSvc" → ...).

**작업 범위**: calibrate 확장 + LLM 호출. **~200줄.**

**의존**: S4-P1 의 LLM 호출 패턴과 일관성 유지.

---

## 비-우선 작업

### S4-P10. 한글 화이트리스트 자동 학습의 학습 코퍼스 옵션화

현재 `build()` 가 후보 청크 전체에서 학습. 별도 코퍼스 (예: 운영 위키 풀텍스트) 에서 학습하면 더 강건.
- `--korean-stopword-corpus <path>` 옵션 추가
- 사전 학습된 stopword 셋을 파일로 저장·재사용

### S4-P11. 그래프 distractor 다양화

`build_synthetic_gold_set.py:402-407` 의 distractor pool 이 단순 셔플 후 `[:n_distractors]`. 더 다양한 distractor 선택 (서로 다른 source_type, 서로 다른 section_path) 으로 일반성 게이트 검출력 향상.

### S4-P12. compare_runs HTML 리포트 출력

stdout + JSON 외에 HTML/markdown 리포트 출력. 사용자가 PR 코멘트에 첨부하기 쉬움.

---

## 다음 세션 컨텍스트 — 빠른 onboarding

### 핵심 파일 위치

| 파일 | 역할 |
|---|---|
| `scripts/build_synthetic_gold_set.py` | 골드셋 생성 (4단계 게이트, 자기-평가 차단, 한글 자동학습) |
| `scripts/eval_search.py` | 검색 채점 (Recall/Precision/MRR/nDCG/Judge 3 모드) |
| `scripts/compare_runs.py` | baseline ↔ treatment paired 비교 (paired_bootstrap, Wilcoxon) |
| `scripts/calibrate_graph_match.py` | 그래프 τ F1 최적점 + `--apply` |
| `src/context_loop/eval/synth.py` | 골드셋 생성 로직 (filter_question, 한글 게이트, korean stopword 학습) |
| `src/context_loop/eval/llm.py` | role 별 LLM 빌더 (`role_is_configured` — endpoint+model 동일성 검사) |
| `src/context_loop/eval/metrics.py` | 표준 메트릭 (변경 없음) |
| `src/context_loop/eval/graph_match.py` | 4-tier cascade (`DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78`) |
| `src/context_loop/eval/gold_set.py` | GoldItem 스키마 (S4-P1 의 anchor 필드 추가 대상) |
| `src/context_loop/processor/llm_client.py` | OpenAI/Endpoint/Anthropic 클라이언트 (S4-P6 대상) |

### 핵심 운영 문서

| 문서 | 내용 |
|---|---|
| [`docs/eval-scripts.md`](./eval-scripts.md) | 4 스크립트 CLI 옵션·예시·통합 워크플로우 |
| [`docs/indexing-improvement-cycle.md`](./indexing-improvement-cycle.md) | 인덱싱 개선 사이클 표준 절차 + content_hash 쿼리 |
| [`docs/setup.md`](./setup.md) | 환경 설정·실행 |
| `_workspace/findings/SUMMARY.md` | v4 (A) 재감사 최종 보고 |
| `_workspace/findings_v3/SUMMARY.md` | v3 (A−) — S2 적용 후 |
| `_workspace/findings_v2/SUMMARY.md` | v2 (B+) — S0/S1 적용 후 |
| `_workspace/findings_prev/SUMMARY.md` | v1 (C) — 초기 진단 |
| `_workspace/patches/S3_SUMMARY.md` | S3 9건 패치 (PR #55) |
| `_workspace/patches/S2_SUMMARY.md` | S2 6건 (PR #54) |
| `_workspace/patches/SUMMARY.md` | S0/S1 12건 (PR #53) |

### 핵심 하네스 (다음 세션에서도 사용)

| 스킬 | 트리거 키워드 | 용도 |
|---|---|---|
| `rag-eval-audit` | "신뢰성 검토", "RAG 평가 감사", "self-evaluation bias" | 4단계 감사 보고서 생성 (v5 감사 시) |
| `rag-eval-fix` | "S4 패치 적용", "감사 결과 기반 개선", "특정 P 항목만 패치" | 패치 적용 + 변경 검증 |
| `eval-gold-set-improvement` | 골드셋·평가 시스템 기능 확장 | 분석→설계→구현 3단계 팀 |

### 다음 세션 시작 시 권장 순서

1. **컨텍스트 확인**: 본 문서 + `_workspace/findings/SUMMARY.md` (v4, A 등급) 읽기
2. **PR 머지 상태 확인**: `gh pr list --state open` — #52~#56 이 머지됐는지
3. **운영 운영 stats 점검** (가능하다면): 골드셋 빌드 한 번 돌려 `fail_korean_leakage` 비율·`fail_non_unique_source` 비율 확인 — 그래프 골드셋 5% 통과율 이슈가 자동 학습으로 개선됐는지
4. **우선 작업 선택**: 위 S4-P1~P9 중 우선순위와 의존성에 따라 선택. P1 (anchor 골드셋) 이 가장 큰 가치
5. **`rag-eval-fix` 하네스 호출**: `/rag-eval-fix` 또는 메인 직접 패치
6. **A → A+ 회복 검증**: `/rag-eval-audit` 으로 v5 감사

### 미해결 운영 이슈 (5% 통과율)

PR #54 작업 중 사용자 보고 — "그래프 골드셋 생성 시 약 5% 만 통과". S3 의 한글 자동 학습으로 `fail_korean_leakage` 는 감소 예상, 그러나 주된 원인이 `fail_non_unique_source` 라면 별도 작업 필요:
- 그래프 모드용 unique_source 게이트 완화 옵션 (`--graph-relax-unique`)
- 또는 그래프 모드 전용 Judge 프롬프트 (그래프 정보 본질이 generic 임을 인정)

**다음 세션 우선 진단**: 운영 stats 의 `fail_*` 분포 확인 → 가장 큰 사유에 맞춤 패치.

### 신뢰도 등급 정의 (참조)

| 등급 | 의미 | 사용 가능 범위 |
|---|---|---|
| A | 운영 출시 게이트 조건부 가능 | 위 4가지 (실패율·judge_mode·variance·p_min_effect) 모니터링 |
| A− | 미세 개선 + 모델 교체 조건부 가능 | 외부 벤치마크는 보수적 |
| B+ | 미세 개선 조건부 가능 | 외부 벤치마크 금지 |
| B | 큰 회귀 검출 + 미세 개선은 N≥10 시 | |
| C | 큰 회귀 검출만 | 외부 벤치마크·운영 게이트 금지 |
| D 이하 | 내부 디버그용만 | |

## 변경 이력

| 날짜 | 작성자 | 내용 |
|---|---|---|
| 2026-05-19 | Claude | 초기 작성 — S3 (PR #55) 완료 시점, A 등급 회복 후 다음 세션 준비 |
