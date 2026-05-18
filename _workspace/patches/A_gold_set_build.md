# Gold-Set Build Patcher — 패치 로그

## 요약

- 적용: **P1, P4, P7, P10, P11** (총 5건, 모두 성공)
- 변경된 파일:
  - `scripts/build_synthetic_gold_set.py`
  - `src/context_loop/eval/synth.py`
- 신규 함수 / CLI 플래그:
  - `--allow-self-eval` (CLI) — 자기 평가 차단 옵트인 해제 플래그
  - `_resolve_eval_role_identity(config, role, *, endpoint_override, model_override)` —
    Generator/Judge 의 effective (model, endpoint) 를 우선순위(CLI > eval.{role} > llm)
    대로 메타데이터 기록용으로 해석.
  - `_unfiltered_output_path(base)` — `--no-filter` 빌드의 출력 경로에 `.UNFILTERED`
    접미사를 강제.
  - `is_unique_source(question, chunk_content, *, judge, reasoning_mode)` (synth.py) —
    "정답 청크가 유일한 출처인지" 판정하는 별도 LLM 게이트. ANSWERABLE 과 의미
    분리.
  - `has_korean_proper_noun_leakage(question, source_text)` /
    `extract_korean_proper_noun_candidates(text)` (synth.py) — 한국어 고유명사
    누설 결정론적 게이트.
- `BLOCKING_CHANGE_FOR_EVAL_PATCHER`: 없음 — `llm.role_is_configured` 시그니처는
  변경하지 않았다 (호출 측만 보강).

---

## P1 — self-eval 차단 + 메타데이터 보강

**변경 위치:**

- `scripts/build_synthetic_gold_set.py`
  - `build()` 시그니처 확장 (origin/main 기준 `:306-330` → 패치 후 `:306-338`)
  - 새 헬퍼 `_resolve_eval_role_identity()` 추가 (`:993-1020`)
  - 새 CLI 플래그 `--allow-self-eval` (`:1083-1089`)
  - 종전 `:1126-1131` 의 `logger.warning(...)` 만 출력하던 fall-through 처리를
    `parser.error(...)` + 진행 시 경고 + effective 식별자 해석으로 교체
    (`:1194-1224`).
  - metadata dict 에 8 개 신규 키 추가 (origin/main `:445-455` → 패치 후
    `:445-495`).
  - `build()` 호출부에 8 개 신규 keyword 인자 전달 (`:1284-1308`).

**diff 요약 (핵심 부분):**

```diff
- if not (gen_configured or judge_configured):
-     logger.warning(
-         "Generator/Judge 모두 system LLM (llm.*) 과 동일 — 자기 평가 편향 가능. ...",
-     )
+ self_evaluation_warning = not (gen_configured or judge_configured)
+ if self_evaluation_warning and not args.allow_self_eval:
+     parser.error(
+         "Generator/Judge 모두 system LLM (llm.*) 과 동일 — 자기 평가 편향이 "
+         "차단되었습니다. ... 실험/디버그 용도로 의도적으로 진행하려면 "
+         "--allow-self-eval 을 명시.",
+     )
+ if self_evaluation_warning:
+     logger.warning("... --allow-self-eval 이 명시되어 진행합니다. ...")
+
+ effective_generator_model, effective_generator_endpoint = (
+     _resolve_eval_role_identity(config, "generator", ...)
+ )
+ effective_judge_model, effective_judge_endpoint = (
+     _resolve_eval_role_identity(config, "judge", ...)
+ )
```

metadata 추가 키 (yaml 직렬화 시 신규 키만 append — 기존 키는 모두 유지):

```python
"generator_model": "<effective_model_id>",
"generator_endpoint": "<effective_endpoint>",
"judge_model": "<effective_model_id>",
"judge_endpoint": "<effective_endpoint>",
"generator_configured_separately": <bool>,
"judge_configured_separately": <bool>,
"self_evaluation_warning": <bool>,
"allow_self_eval": <bool>,
```

**테스트 / 회귀 확인 포인트:**

- `python scripts/build_synthetic_gold_set.py --help` → `--allow-self-eval`
  플래그 표시 여부 확인.
- 옵션 없이 + system LLM 만 구성된 config 로 실행 → `parser.error()` 가 발생하여
  exit code 2 + stderr 메시지. CI 가 silent self-eval 빌드를 못 만든다.
- `--allow-self-eval` 명시 또는 `config.eval.{generator,judge}.*` 채워서 실행 →
  기존 흐름과 동일하게 동작.
- 산출된 yaml 의 `metadata.generator_model` / `metadata.judge_model` 확인 →
  실제 모델 ID 가 박혀 있어야 한다.

---

## P4 — GENERIC 프롬프트 분리

**변경 위치:** `src/context_loop/eval/synth.py`

- `GENERIC_PROMPT_TEMPLATE` 본문 교체 (origin/main `:136-146` → 패치 후
  `:136-152`).
- 신규 헬퍼 `is_unique_source()` 추가 (`:650-676`).
- `filter_question()` 의 일반성 게이트 단계 분리 (origin/main `:599-622` →
  패치 후 `:679-747`). distractor 루프 직전에 LLM 호출 1회로 "유일성" 을 별도
  검증하는 (d1) 단계를 추가하고, 기존 distractor 루프는 (d2) 보조 검증으로
  남긴다.
- `FilterReport.reason` 가능값 docstring 갱신 — `non_unique_source` 추가
  (`:75-86`).

**diff 요약:**

```diff
 GENERIC_PROMPT_TEMPLATE = """\
 질문: {question}

 문맥:
 ---
 {chunk_content}
 ---

- 이 문맥만 보고 위 질문에 사실 기반으로 답할 수 있는가?
+ 이 문맥이 위 질문에 대한 **유일한 정답 출처**라고 단정할 수 있는지 평가하라.
+
+ 판단 기준:
+ - 문맥에 명시되지 않은 정보로 답해야 한다면 'no'
+ - 다른 일반적인 문서/매뉴얼/위키에서도 같은 답을 얻을 수 있다면 'no'
+ - 이 문맥에만 있는 고유한 정보로 답해야만 한다면 'yes'
+
 yes/no 한 단어로만 답하라.
 """
```

`filter_question()`:

```diff
+ # (d1) 유일성 — 정답 청크가 유일한 출처인지 확인. ``is_answerable`` 과
+ # 의미가 명확히 분리된 프롬프트(GENERIC_PROMPT_TEMPLATE)로 호출.
+ unique = await is_unique_source(question, source_chunk, ...)
+ if unique is None:
+     return FilterReport(passed=False, reason="parse_error")
+ if not unique:
+     return FilterReport(passed=False, reason="non_unique_source")

- # (d) 일반성 — 무관 청크로도 답할 수 있으면 정답 청크 유일성이 깨짐
+ # (d2) Distractor 보조 검증 — 무관 청크로도 답할 수 있으면 정답 청크 유일성이 깨짐
 for distractor in distractors:
     ans = await is_answerable(...)
```

**테스트 / 회귀 확인 포인트:**

- 청크 본문을 거의 그대로 베낀 질문이 `non_unique_source` 로 탈락하는지 (Judge
  가 yes/no 를 다른 의미로 평가하므로 self-bias 영향이 분산된다).
- 기존 distractor pool 기반 generic 탈락은 그대로 작동 — (d1) 통과한 질문만
  (d2) 보조 검증을 받는다.
- `LLMClient.complete(purpose=...)` 호출이 새 purpose `goldset_judge_unique_source`
  도 받아야 한다. 로깅 필터에서 unknown purpose 를 거르는 곳이 없는지 확인.

---

## P7 — 한글 누설 게이트

**변경 위치:** `src/context_loop/eval/synth.py`

- 새 결정론 게이트 모듈 블록 추가 (origin/main `:299` 의 ASCII-only
  `_IDENT_RE` 옆에 별도 섹션 — 패치 후 `:353-411`).
- `_KOREAN_NOUN_RE`, `_KOREAN_COMMON_NOUNS`, `extract_korean_proper_noun_candidates()`,
  `has_korean_proper_noun_leakage()` 신규 추가.
- `filter_question()` 의 결정론 게이트 (b) 다음에 (b2) 한국어 누설 게이트로
  연결 (`:719-722`).
- `FilterReport.reason` 가능값에 `korean_leakage` 추가.

**옵션 선택:** 정의 파일에서 권장한 **옵션 B** (별도 함수) 채택. 영문 식별자
검사와 분리해 false positive 영향이 적고, 한국어 빈도 컷오프로 일반 어휘를
자연 필터한다.

**diff 요약:**

```diff
+ _KOREAN_NOUN_RE = re.compile(r"[가-힣]{4,}")
+ """4자 이상의 연속 한글 — 일반 2~3자 명사는 stopword 화이트리스트 보강
+ 없이 길이로 자연 필터."""
+
+ _KOREAN_COMMON_NOUNS = frozenset({
+     "사용자가", "사용자는", ...,  # 4자 이상이라도 너무 흔한 어휘
+ })
+
+ def extract_korean_proper_noun_candidates(text, *, max_freq=1) -> set[str]:
+     """4자 이상 한글 + 빈도 max_freq 이하 + stopword 제외."""
+
+ def has_korean_proper_noun_leakage(question, source_text) -> bool:
+     """질문이 출처 청크의 한국어 고유명사를 그대로 베꼈는지 검사."""
+
 # filter_question() 내부
 if has_identifier_leakage(question, source_chunk):
     return FilterReport(passed=False, reason="leakage")
+ if has_korean_proper_noun_leakage(question, source_chunk):
+     return FilterReport(passed=False, reason="korean_leakage")
 if has_demonstrative_reference(question):
     return FilterReport(passed=False, reason="demonstrative")
```

**테스트 / 회귀 확인 포인트:**

- 한국어 청크에 "결제한도처리" 같은 4자 이상 고유명사가 청크 내 1회만
  등장하고, 질문에 그대로 들어가면 `korean_leakage` 로 탈락.
- 일반 어휘("사용자가", "프로젝트") 는 화이트리스트로 제외됨.
- 빈도 컷오프(`max_freq=1`)로 청크에 자주 등장하는 단어는 후보에서 자연 제외.
- `build()` 의 stats dict 에 `fail_korean_leakage` 카운터가 추가되어 탈락
  분포가 관찰된다.

---

## P10 — 그래프 evidence DB fallback 제거

**변경 위치:** `scripts/build_synthetic_gold_set.py:_make_graph_gold_item()`

- origin/main `:866` 의 `description = gq.evidence_description or str(sg.get("entity_description") or "")`
  → 패치 후 `description = gq.evidence_description` (`:909`).
- docstring 에 변경 사유 보강 — DB 폴백 제거로 T4 trivial 매칭 차단 및
  `_embed_graph_item_descriptions` 가 빈 description 을 자동 skip 한다는
  계약 명시 (`:897-906`).

**diff 요약:**

```diff
 def _make_graph_gold_item(sg, gq, *, score_relations=False) -> GoldItem:
     """...

+   감사 보강: LLM 이 evidence_description 을 비우면 description 도 빈 문자열로
+   둔다. graph_store 의 원본 entity_description 으로 폴백하지 않는다 —
+   인덱싱 시점 description 을 그대로 정답으로 사용하면 T4 임베딩 cosine 이
+   trivially 1.0 으로 부풀려져 그래프 시스템의 표기 변형·패러프레이즈 강건성
+   측정이 무력화된다. description 이 비면 임베딩 단계가 자연 skip 한다.
     """
-    # 그래프 노드 자체 description 을 fallback evidence 로 사용.
-    description = gq.evidence_description or str(sg.get("entity_description") or "")
+    # LLM 이 자연어로 풀어쓴 evidence 만 사용. 빈 문자열이면 T4 skip.
+    description = gq.evidence_description
```

**계약 확인:** `_embed_graph_item_descriptions()` 의 빈 description skip 로직이
이미 존재 (`if entity.description and entity.description_embedding is None`,
`:956`). 따라서 description 이 비면 임베딩 배치에 포함되지 않고, 평가 측
`graph_match.py` 의 T4 단계도 description 이 없는 entity 는 임베딩을 만들지
않아 자연 skip 된다.

**테스트 / 회귀 확인 포인트:**

- LLM 이 `evidence_description` 을 채워 보내는 정상 경로는 동작 유지 (LLM
  품질에 의존하던 부분이 명시화됨).
- LLM 이 빈 evidence 를 보내면 골드셋 항목의 `relevant_graph_entities[0].description`
  이 빈 문자열로 저장되며, 평가 시 T4 매칭 미수행 (T1~T3 만 작동).
- 빌드 후 yaml 에서 `relevant_graph_entities[].description` 이 빈 항목 수를
  세어 LLM evidence 품질 지표로 활용 가능.

---

## P11 — `--no-filter` 출력 경로 분리

**변경 위치:** `scripts/build_synthetic_gold_set.py`

- 신규 헬퍼 `_unfiltered_output_path(base)` 추가 (`:991-1006`).
- `main()` 의 `base_output = Path(args.output)` 다음에 `args.no_filter` 일 때
  강제 변환 로직 추가 (`:1257-1268`).
- `--no-filter` CLI help 텍스트 갱신 — `.UNFILTERED` 접미사 강제 안내
  (`:1078-1080`).

**diff 요약:**

```diff
 def _unfiltered_output_path(base: Path) -> Path:
+    """--no-filter 빌드 경로에 .UNFILTERED 접미사를 강제 부여한다.
+    eval/gold_set.yaml → eval/gold_set.UNFILTERED.yaml.
+    이미 .UNFILTERED 가 포함되면 이중 접미사 방지."""
+    stem = base.stem
+    if stem.endswith(".UNFILTERED"):
+        return base
+    return base.with_name(f"{stem}.UNFILTERED{base.suffix}")

 base_output = Path(args.output)
+if args.no_filter:
+    original_output = base_output
+    base_output = _unfiltered_output_path(base_output)
+    if base_output != original_output:
+        logger.warning(
+            "--no-filter 빌드 — 출력 경로를 %s 에서 %s 로 변환했습니다. ...",
+            original_output, base_output,
+        )
```

**`_numbered_output_path` 와의 호환성:** `--n-gold-sets > 1` + `--no-filter`
조합 시 처리 순서:

1. `base_output = eval/gold_set.yaml` → `_unfiltered_output_path` 적용 →
   `eval/gold_set.UNFILTERED.yaml` 로 변환.
2. `_numbered_output_path(eval/gold_set.UNFILTERED.yaml, i, total)` →
   `eval/gold_set.UNFILTERED_001.yaml`, `..._002.yaml`, ...

UNFILTERED 가 stem 끝에 위치하고 `_NNN` 이 그 뒤에 붙어 모든 파생 파일이
운영 골드셋과 시각적으로 분리된다.

**테스트 / 회귀 확인 포인트:**

- `--no-filter --output eval/gold_set.yaml` → 실제 출력 `eval/gold_set.UNFILTERED.yaml`,
  stderr 에 변환 경고 1회.
- `--no-filter --n-gold-sets 3` → `_001`, `_002`, `_003` 접미사도 정상 부여.
- `--no-filter --output already.UNFILTERED.yaml` → 이중 접미사 안 붙음.
- 평가 스크립트(`scripts/eval_search.py`) 가 `metadata.filter_applied=False`
  또는 파일명에 `.UNFILTERED` 가 포함된 골드셋을 거부하도록 강화하는 작업은
  P2 ranges (eval-script-patcher) 가 담당.

---

## 회귀 위험 점검

### 기존 사용자 흐름 영향

| 사용자 시나리오 | 패치 전 동작 | 패치 후 동작 |
|---|---|---|
| `--generator-* / --judge-*` 또는 `config.eval.*` 채운 빌드 | 정상 진행 | 정상 진행 — metadata 신규 키만 추가 |
| 옵션 없이 system LLM 만 + 옵션 없음 | 경고 한 줄 후 진행 | **`parser.error`로 종료** — 사용자가 `--allow-self-eval` 명시해야 진행 |
| `--no-filter` 디버그 빌드 | `eval/gold_set.yaml` 덮어쓰기 | `eval/gold_set.UNFILTERED.yaml` 로 자동 변환 + stderr 경고 |
| 그래프 모드에서 LLM 이 evidence 빈 응답 | DB description 으로 fallback (trivial 매칭 위험) | description 빈 문자열 → T4 skip |
| Generator 가 한국어 청크 명사를 베껴 질문 작성 | 검출 안 됨 | `fail_korean_leakage` 로 탈락 |
| Generator 가 거의 청크 그대로 베껴 질문 작성 | (a)/(d) 동일 프롬프트로 양쪽 통과 가능 | (d1) 유일성 게이트가 `non_unique_source` 로 탈락시킴 |

**의도적 차단 두 건 (P1, P11):**

- **P1**: self-eval fall-through 차단은 CI/CD 무인 실행에서 발생하는 silent
  편향을 막기 위해 의도적으로 도입한 breaking change. 옵트인(`--allow-self-eval`)
  으로 운영자 명시 동의가 필요.
- **P11**: `--no-filter` 출력 분리는 운영 골드셋 오염을 막기 위한 path
  rewrite. 사용자 의도(디버그 빌드)에 맞추되 파일명만 안전화.

### 메타데이터 yaml 호환성

- 기존 metadata 키: `generated_at`, `n_chunks_sampled`, `questions_per_chunk`,
  `filter_applied`, `seed`, `source_types`, `generation_modes`, `concurrency`,
  `stats`, `embedding_model`, `graph_match_threshold_default`,
  `score_relations`, `embed_graph_evidence` → **모두 유지**.
- 신규 키 8 개 추가 → `GoldSet.metadata` 가 `dict[str, Any]` 이므로
  `gold_set.py` 의 `save_gold_set` 직렬화에 부담 없음.
- 평가 측이 신규 키를 모르더라도 yaml 로드 시 무시 — 후방 호환.
- stats 신규 카운터: `fail_korean_leakage`, `fail_non_unique_source`,
  `fail_demonstrative` 가 dict 에 추가됐다. `_merge_stats` 는 동적 키 합산을
  이미 지원하므로 안전.

### `filter_question` LLM 호출 수 증가

- 패치 전: 1 + N (= answerable 1회 + distractor N회).
- 패치 후: 2 + N (= answerable 1회 + unique_source 1회 + distractor N회).
- 빌드 시간이 약 20~30% 증가할 수 있다 (LLM 호출 한 건 추가). 자기 평가
  편향 완화의 트레이드오프.
- `--concurrency` 로 일부 상쇄 가능.

### 빈 evidence 그래프 항목의 평가 영향

- T4 임베딩 매칭이 미수행되므로 그래프 recall 이 보수적으로 낮게 측정될 수
  있다 — 이는 의도된 행동 (예전엔 trivial 1.0 hit 이 부풀렸음).
- 평가 측에서 description 비율을 보고하는 작업은 `eval-script-patcher` 영역.

### 검토되지 않은 모듈 영향 (없음 확인)

- `llm.py` 의 `role_is_configured`: 시그니처 미변경.
- `gold_set.py` 의 `GoldItem` / `GoldSet`: 새 필드 없음, metadata dict 만 확장.
- `graph_match.py`: 임베딩 skip 로직 이미 존재 (`description` 빈 문자열 처리).

---

## 추가 메모

- 인라인 주석에 P 번호는 인용하지 않았다. 변경 사유는 본 패치 로그에 기록.
- 한국어 docstring/주석 컨벤션 유지. 신규 코드의 모든 docstring 은 한국어.
- 영향 받는 줄 번호는 패치 후 기준 — origin/main 대비 모든 변경 위치는 위
  표·diff 에 명시.
- 적용 실패한 항목: **없음**. P1·P4·P7·P10·P11 모두 성공적으로 적용.
