# 03_implementation — 문서 기반 골드셋 생성 전환 구현 (R1/R2/R3)

작성: 2026-05-22 (implementer, eval-gold-set-improvement)
기준: 02_design.md 지시서 + 사용자 확정 결정(대체/통째/그래프 보강, D6-1~D6-5)

> 변경 이력: 이전 03_implementation(PR#65 cross-doc/answer-equivalence)을 본 작업
> (문서 기반 전환) 스코프로 전면 교체. 설계 문서(02_design.md)가 전면 교체된 것과 동일.

---

## 1. 변경 파일 목록

| 파일 | 종류 | 핵심 변경 |
|------|------|-----------|
| `src/context_loop/eval/synth.py` | 수정 | `count_tokens` import; `truncate_to_tokens` public 헬퍼 신설(§3.2); `GRAPH_GENERATE_PROMPT_TEMPLATE` 에 `{document_excerpt}` 슬롯 추가(§4.2); `generate_graph_questions` 에 `doc_max_tokens` 파라미터 + 보강 원문 슬롯 채움 + truncate(§4.2-4.3); stopword docstring 문구 갱신. |
| `scripts/build_synthetic_gold_set.py` | 수정 | `load_candidate_chunks`→`load_candidate_documents`(chunks 비의존, §1); `DISTRACTOR_EXCERPT_CHARS`+`_distractor_excerpt`(§2.2); build() 로더 호출/distractor 풀 키 `chunk_id`→`document_id`/stopword `min_corpus_freq` 5→8(§2.1,§5.4); seed `chunk_index`→`document_id`(§1.4); `source_section_path`=title(§1.5); R2 truncate 가드 적용 + `truncated_too_large` stats(§3.3-3.4); sg dict 에 `primary_document_content`(§4.1); `max_doc_tokens` plumbing(build/_run_chunk_mode/_process_chunk_item/_run_graph_mode/_process_subgraph_item); CLI `--max-doc-tokens` 신규 + `--n-chunks`/`--min-chars`/`--max-chars`/`--n-distractors` help·기본값(§5.2); 로그 `[chunk …]`→`[doc …]`; 모듈 docstring 사용법 갱신. |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정 | `load_candidate_chunks` 테스트 2건 → `load_candidate_documents` 테스트 5건으로 재작성; `_distractor_excerpt` 테스트; subgraph `primary_document_content` 테스트 2건. |
| `tests/test_eval/test_concurrency.py` | 수정 | chunk dict → 문서 dict 스키마(document_id/source_type/content/title/url)로 교체(전 sampled/chunk 픽스처); `source_section_path==title` 검증; R2 truncate stats + anchor 추적성 + truncate-비활성 테스트 2건 추가. |
| `tests/test_eval/test_synth.py` | 수정 | `GRAPH_GENERATE_PROMPT_TEMPLATE.format` 에 `document_excerpt` 슬롯 반영; `truncate_to_tokens` 3건 + graph 보강(document_excerpt 주입/placeholder/truncate) 3건 추가. |
| `src/context_loop/eval/gold_set.py` | **무변경** | GoldItem 스키마 불변(D5). |
| `src/context_loop/processor/chunker.py` | **무변경** | `count_tokens` 재사용만. |

---

## 2. 사용자 확정 결정 반영 확인

1. **대체** — `load_candidate_chunks` 완전 삭제, `load_candidate_documents`(original_content 기반, `get_chunks_by_document` 미호출)로 교체. chunks 테이블 비의존.
2. **통째 + 한도 가드** — generator 입력은 문서 원문 전체. `--max-doc-tokens` 기본 24000, 초과 시 앞부분 truncate(skip 아님). `truncated_too_large` stats 기록. `--max-chars` 기본 200000 으로 상향(통째 정책 — 큰 문서 후보 보존).
3. **그래프 보강** — sg dict 에 `primary_document_content`(소유 문서 원문, 추가 DB 호출 없음) 적재 → `generate_graph_questions` 프롬프트의 "소유 문서 발췌" 슬롯에 주입. judge 게이트는 `subgraph_snippet` 그대로 유지(D6-4).
4. **D6-1** stopword `min_corpus_freq` 5→8. **D6-2** section_path→title. **D6-3** truncate/24000. **D6-4** judge subgraph_snippet 유지. **D6-5** distractor 앞부분 2000자 prefix.

---

## 3. 테스트 결과

### pytest tests/test_eval/
```
5 failed, 304 passed
```
변경 전 baseline: 5 failed, 291 passed. 신규/재작성 테스트가 통과하며 신규 실패 0.

### 선재 실패 vs 신규 실패 (git stash 대조)
작업 트리 전체를 `git stash` 한 뒤 동일 5건 테스트 실행 → 5건 모두 변경 전에도 실패 확인.
**선재 실패 5건 (PR#65 시점부터 존재, 본 작업과 무관 — 손대지 않음):**
- `test_build_synthetic_gold_set.py::test_fetch_source_text_anchor_match`
- `test_build_synthetic_gold_set.py::test_fetch_source_text_legacy_chunk_id_fallback`
- `test_build_synthetic_gold_set.py::test_make_graph_gold_item_falls_back_to_node_description`
- `test_synth.py::test_filter_question_passes_clean`
- `test_synth.py::test_filter_question_fails_generic`

**신규 실패: 0건.** 본 변경으로 새로 깨진 테스트 없음.

### 추가/재작성한 테스트 (시나리오)
- `test_load_candidate_documents_uses_original_content` — original_content 기반 로드, 청크 없어도 동작, chunk 전용 키 부재.
- `test_load_candidate_documents_char_filter` — min/max_chars 가 문서 길이 기준.
- `test_load_candidate_documents_skips_empty_content` — NULL/공백 본문 제외.
- `test_load_candidate_documents_filters_source_types` — source_type 화이트리스트.
- `test_load_candidate_documents_sorted_by_document_id` — document_id 오름차순(결정론).
- `test_distractor_excerpt_truncates_to_constant` — DISTRACTOR_EXCERPT_CHARS 절단.
- `test_load_candidate_subgraphs_basic`(키 존재 assert 추가) + `test_load_candidate_subgraphs_includes_primary_document_content` — R3 sg dict 보강.
- `test_process_chunk_item_*`(2건) — 문서 dict 스키마, source_section_path==title, R2 truncate stats + anchor 원문 추적성 + truncate 비활성.
- `test_generate_graph_questions_injects_document_excerpt` / `_no_document_content_placeholder` / `_truncates_document_excerpt` — R3 프롬프트 주입/placeholder/truncate.
- `test_truncate_to_tokens_disabled` / `_under_limit` / `_over_limit` — R2 헬퍼 경계.
- judge 입력 보존: `_process_subgraph_item` 기존 테스트가 `subgraph_snippet` 게이트 입력을 그대로 유지(보강 원문 미포함) — 무영향 통과로 D6-4 확인.

### ruff
변경 파일 5개 ruff check → **신규 코드 클린**. 잔여 E501 1건은 `--source-types` help(라인 1534)로 **선재 E501**(git stash 대조로 변경 전에도 동일 1건 존재). 무시.

### CLI 확인
`python scripts/build_synthetic_gold_set.py --help` → `--max-doc-tokens`(기본 24000), `--n-chunks`(문서 수), `--max-chars`(기본 200000) 노출 확인.

### collateral
`tests/test_processor/test_chunker.py` + `tests/test_eval/` → 336 passed, 5 failed(동일 선재). import 결합(synth→chunker) 부작용 없음.

---

## 4. 설계-구현 불일치 / 비고

1. **`count_tokens` 폴백 동작**: tiktoken 부재 폴백은 02_design 의 "1char=1token" 서술과 일치(`_FALLBACK_CHARS_PER_TOKEN=1`). `truncate_to_tokens` while 루프가 폴백/실측 모두에서 한도 이하로 수렴함을 `test_truncate_to_tokens_over_limit` 로 검증.
2. **함수 리네임 미적용(설계 §1.7 — 선택)**: `_process_chunk_item`/`_run_chunk_mode` 이름 유지(테스트 import 호환). docstring 으로 "문서 1건 처리"임을 명시. 내부 로그만 `[doc …]` 로 변경.
3. **build()-레벨 distractor 풀 제외 테스트**: 풀 분리(`document_id` 기준 제외)는 build() 인라인 코드라 LLM 전체 mock 없이 단위 호출 불가. `_distractor_excerpt`(prefix 절단) 단위 테스트로 D6-5 의 검증 가능한 핵심을 커버. 풀 분리 키 변경 자체는 코드로 확인(§2.1, chunk_id→document_id).
4. **`--max-doc-tokens` 기본값 24000(D6-3, 위험 §9.1)**: 사내 generator endpoint context window 미상. 운영 endpoint 가 작으면(예 8k) truncate 빈발 → metadata.stats.truncated_too_large 로 사후 확인 후 CLI 조정 권장.
5. **`_workspace/00,01,02.md` 작업 트리 변경**: 본 implementer 가 수정하지 않음(읽기 전용). git diff 에 나타나는 변경은 본 작업 외 출처.

---

## 5. 새 CLI 사용 예시

```bash
# 문서 기반 골드셋 (통째 입력, 24000 토큰 초과 시 앞부분 truncate)
python scripts/build_synthetic_gold_set.py \
    --source-types confluence_mcp,git_code \
    --n-chunks 50 \
    --max-chars 200000 \
    --max-doc-tokens 24000 \
    --output eval/gold_set.yaml

# endpoint context window 가 작을 때 한도 하향
python scripts/build_synthetic_gold_set.py \
    --n-chunks 30 --max-doc-tokens 8000 \
    --output eval/gold_set.yaml

# graph 모드 — 소유 문서 원문 보강 자동 적용 (max-doc-tokens 공유)
python scripts/build_synthetic_gold_set.py \
    --include-graph-questions --n-graph-nodes 20 \
    --max-doc-tokens 16000 \
    --output eval/gold_set.yaml

# 토큰 가드 비활성 (무제한 — 입력 한도 확실할 때만)
python scripts/build_synthetic_gold_set.py --max-doc-tokens 0
```
