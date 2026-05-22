# 01_analysis — 문서 기반 골드셋 생성 전환 분석 (R1/R2/R3)

작성: 2026-05-22 (analyst, eval-gold-set-improvement)
기준: 현재 HEAD (PR#65 stack — `relevant_doc_groups` 이미 반영됨)
요구: `_workspace/00_requirements.md` (R1 대체 / R2 입력 한도 / R3 그래프 보강)

분석 대상 코드(모두 정독):
- `scripts/build_synthetic_gold_set.py`
- `src/context_loop/eval/synth.py`
- `src/context_loop/eval/gold_set.py`
- `src/context_loop/storage/metadata_store.py`
- `src/context_loop/processor/chunker.py` (`count_tokens`)
- `tests/test_eval/test_build_synthetic_gold_set.py`, `test_concurrency.py`, `test_synth.py`

추측 없음. 모든 근거는 파일:라인 인용.

---

## 0. 현재 chunk 모드 데이터 흐름 (사실)

```
build()                                            scripts/build_synthetic_gold_set.py:393
 └─ load_candidate_chunks(store, ...)              :474  ← R1 대체 대상
     · store.list_documents()                      :156
     · for doc: store.get_chunks_by_document(id)    :163  ← chunks 테이블 의존
     · min/max_chars 필터 (len(content))            :166
     · dict{chunk_id,chunk_index,document_id,        :168-176
            source_type,content,section_path,title}
     · sort by (document_id, chunk_index)           :178
 └─ stratified_sample(candidates, key=source_type)  :483
 └─ distractor_pool = candidates - sampled (chunk)   :491-495
 └─ build_korean_stopwords_from_corpus([c.content])  :499
 └─ _run_chunk_mode(...)                              :532
     └─ _process_chunk_item(idx, chunk, ...)          :650 / 797
         · item_seed = seed_base + chunk_index        :682  ← R1 결정성 키
         · generate_questions(chunk["content"], ...)  :687
         · same_type_distractors (source_type 필터)    :704-710
         · anchor = make_text_anchor(chunk["content"]) :712
         · filter_question(q, chunk["content"],         :735
              [d["content"] for d in distractors])     :738
         · GoldItem(relevant_doc_ids=[document_id],     :716-726 / 753-763
              source_document_id, source_text_anchor=anchor,
              source_section_path=section_path)
```

graph 모드(`_run_graph_mode` :950) 와 cross-doc 모드(`_run_cross_doc_mode` :1174)
는 chunk 모드 **이후** 별도로 실행되며 `next_id` 만 이어받는다(:552, :579).

---

## 1. R1 — 대체 영향 범위 (load_candidate_chunks → 문서 기반 로더)

### 1.1 호출부 / 하류 의존 (전부)

| 위치 | 파일:라인 | 사용 내용 | 대체 시 처리 |
|------|-----------|-----------|--------------|
| 호출 1곳 | `build()` `scripts/build_synthetic_gold_set.py:474` | `candidates = await load_candidate_chunks(...)` | 문서 로더 호출로 교체. 시그니처(`store, source_types, min_chars, max_chars`) 유지 가능 — `min/max_chars` 의미가 청크→문서로 바뀜(R1) |
| 빈 후보 가드 | :480-481 | `if not candidates: raise RuntimeError("후보 청크가 없습니다…")` | 메시지만 "후보 문서" 로. 로직 동일 |
| 샘플링 | :483-485 | `stratified_sample(candidates, key="source_type")` | dict 에 `source_type` 키만 있으면 무변경 |
| distractor 풀 | :491-495 | `sampled_chunk_ids = {s["chunk_id"]}` → `distractor_pool = [c if c["chunk_id"] not in ...]` | **`chunk_id` 키 의존 → `document_id` 로 변경**(§1.4) |
| 한글 stopword 학습 | :499-503 | `build_korean_stopwords_from_corpus([c["content"] for c in candidates])` | `content` 키만 있으면 무변경. 단 코퍼스가 문서 전체로 커져 빈도/메모리 특성 변화(미해결 Q1) |
| `_run_chunk_mode` | :532-548 | `sampled`, `distractor_pool` 전달 | 무변경(전달만) |
| `_process_chunk_item` | :650-770 | chunk dict 키 다수 사용 | §1.2 상세 |

`stratified_sample` (`synth.py:1002`) 은 `c.get(key, "_unknown")` 만 읽으므로
`source_type` 외 키에 비의존 — **dict 키 이름이 chunk→document 로 바뀌어도 무변경**.

### 1.2 `_process_chunk_item` 이 chunk dict 에서 읽는 키 전부 (`:650-770`)

| 키 | 사용 라인 | 용도 | 문서 dict 로 대체 시 |
|----|-----------|------|---------------------|
| `content` | :687(generate), :712(anchor), :738(distractor 본문) | Generator 입력 + anchor + distractor 본문 | **`original_content` 로 대체**(R1 통째 입력) |
| `chunk_index` | :675(log), :682(seed) | 결정성 seed 키 + 로그 | **사라짐. `document_id` 로 대체**(§1.3) |
| `document_id` | :675(log), :719/756(`relevant_doc_ids`), :720/759(`source_document_id`) | 정답 doc + 출처 | **그대로 사용**(문서 dict 에도 존재). 정답 스키마 불변 |
| `source_type` | :676(log), :704/706(distractor 필터), :721/760(`source_type`) | distractor 동일 타입 우선 + 메타 | **그대로**(문서 dict 에 존재) |
| `chunk_id` | (`_process_chunk_item` 내 직접 사용 **없음** — 풀 분리는 build() :491) | — | 함수 자체는 `chunk_id` 미사용. build() 풀 분리만 영향 |
| `section_path` | :723/762(`source_section_path`) | 디버깅용 메타 | **문서엔 청크 단위 섹션 경로 없음. 사라짐** → `""` 또는 문서 title/url(미해결 Q2) |
| `title` | (chunk dict 에 있으나 `_process_chunk_item` 미사용) | — | 무영향 |

→ **사라지는 것**: `chunk_index`(seed/정렬), `section_path`(청크 섹션 경로).
→ **반드시 대체**: seed 키(`chunk_index`→`document_id`), distractor 풀 분리 키
(`chunk_id`→`document_id`), Generator/anchor/distractor 본문 소스
(`content`→`original_content`).

### 1.3 deterministic seed (chunk_index 기반, :682)

```python
item_seed = generator_seed_base + int(chunk.get("chunk_index") or 0)  # :682
judge_seed = item_seed + 10000 + j                                     # :732
```
- `chunk_index` 가 문서 dict 에 없음. 문서는 1 doc = 1 후보 → **`document_id` 가
  자연스러운 안정 seed 키** (문서당 1회 처리, 충돌 없음).
- `_run_chunk_mode` 의 `id` 부여(:826)는 idx 순서 기반이라 seed 와 무관 — 무영향.
- 비기능요구 "문서 단위 deterministic seed 유지"(00_requirements.md:49) 충족 가능:
  `item_seed = generator_seed_base + document_id`.

### 1.4 distractor_pool (:491-495)

```python
sampled_chunk_ids = {s["chunk_id"] for s in sampled}              # :491
distractor_pool = [c for c in candidates
                   if c["chunk_id"] not in sampled_chunk_ids]      # :492-494
```
- `chunk_id` 로 "샘플과 다른 항목" 구분 → 문서 모드에서는 **`document_id`** 로 구분.
- `_process_chunk_item` 내 distractor 사용(:704-710): 같은 `source_type` 우선,
  부족분 다른 타입 충당. 문서 dict 도 동일 키로 동작.
- distractor 본문 입력(:738): `[d["content"] for d in same_type_distractors]`
  → 문서 모드에서는 `d["original_content"]`. **distractor 가 통째 문서가 되면
  judge `is_answerable` 호출당 입력 토큰 급증**(R2 가드 동일 이슈 — §3, 미해결 Q5).

### 1.5 source_text_anchor 재정의 (:712)

```python
anchor = make_text_anchor(chunk["content"])  # :712
```
- `make_text_anchor` (`synth.py:277`) 는 whitespace 정규화 후 앞 200자 prefix.
- 문서 원문에 적용해도 **로직상 문제 없음** — 임의 텍스트 prefix 추출(테스트
  `test_synth.py:468-490` 확인). 문서 원문 앞부분이 anchor → 추적성 유지
  (00_requirements.md:32 충족).
- 단 anchor 가 "문서 도입부" 만 가리키게 됨 — 청크 단위로 본문 위치를 좁히던
  추적성은 약화(디버그 용도, 채점 키 아니므로 기능 영향 없음).

### 1.6 load_candidate_chunks 자체 구현 (대체 대상, :132-183)

- `store.get_chunks_by_document` (`metadata_store.py:349`) 의존 = **chunks 테이블 의존**.
  R1 핵심 제거 대상.
- 대체 로더는 `store.list_documents()` (`metadata_store.py:233`, `SELECT *`)
  결과의 `original_content` 직접 사용 가능 — chunks 테이블 완전 비의존.
  `list_documents` 는 모든 컬럼 반환(documents 스키마 `metadata_store.py:17-33`):
  `id, source_type, title, original_content, content_hash, ... url, author`.
- 필터 재정의: `len(original_content)` 기준 min/max_chars(문서 단위).
  `original_content` 가 `NULL`/빈 문자열인 문서 가드 필요(스키마상 nullable
  `metadata_store.py:22`).

---

## 2. R1 — distractor 게이트 의미 변화

### 2.1 filter_question 의 distractor 사용 (`synth.py:911-994`)

게이트 순서(`:942-994`):
1. (a) `is_answerable(q, source)` — LLM (:944)
2. (b1) ASCII 식별자 누출 — 결정론 (:954)
3. (b2) 한국어 고유명사 누출 — 결정론 (:960)
4. (c) 지시대명사 — 결정론 (:968)
5. (d1) `is_unique_source(q, source)` — LLM (:974)
6. (d2) **distractor 루프**: 각 distractor 에 `is_answerable(q, distractor)`,
   하나라도 `True` 면 `generic` 탈락 (:984-992)

→ distractor 는 (d2) 단일 용도: **"무관 항목으로도 답이 되면 generic"** = 정답
출처 유일성 보조 검증. `make_text_anchor` 와 무관(anchor 는 게이트에 안 들어감).

### 2.2 청크 distractor → 문서 distractor 의미 변화

- 현재: distractor = "다른 청크". build() :492 가 `chunk_id` 만 제외하고
  **`document_id` 는 제외 안 함** → **자기 문서의 옆 청크가 distractor 가 되는
  케이스 존재**. 같은 문서 내용이라 `is_answerable=True` 로 과도하게 generic
  탈락시킬 잠재 약점(현 코드).
- 문서 distractor 로 바뀌면: distractor = "다른 문서" 통째. 정답 문서와 명확히
  분리(같은 doc 가 distractor 가 될 수 없음). **게이트 의미가 "다른 문서로도 답
  가능하면 generic" 으로 더 깨끗해짐** — doc 단위 채점과 정합. R1 의도와 일치.
- 부작용: distractor 가 통째 문서 → (d2) `is_answerable` LLM 입력 급증(§3, 미해결 Q5).
  distractor 개수(`n_distractors` 기본 2) × 큰 입력 = judge 토큰 비용 증가.

### 2.3 make_text_anchor 의 문서 원문 적용 (재확인)

- `make_text_anchor` 는 게이트(`filter_question`)에 입력되지 **않음** — GoldItem 의
  `source_text_anchor` 필드에만 들어감(`:712,759`). 게이트 의미와 무관.
- 문서 원문 적용 시 로직 안전(§1.5). **문제 없음.**

---

## 3. R2 — generator 입력 한도 현황

### 3.1 현재 입력 가드 현황 (사실)

- **build 경로에 토큰 기반 입력 가드 없음.** 유일한 크기 제한:
  - chunk 모드: `min_chars`/`max_chars` (기본 200/8000자, CLI `:1530-1537`,
    필터 위치 `load_candidate_chunks` :166). **문자 기준, 청크 단위.**
  - graph 모드: `GRAPH_SNIPPET_MAX_CHARS=8000` (`synth.py:266`),
    `build_subgraph_snippet` 가 8000자 초과 시 절단(`synth.py:324-325`).
- `generate_questions` (`synth.py:718`) 는 `chunk_content` 를 그대로 프롬프트에
  format(:737) — **입력 길이 검사 없음**. `max_tokens=1024` 는 출력 한도일 뿐.
- 청크는 max_chars=8000(약 2000~3000 토큰, `:154` 주석)으로 작아 문제 없었음.

### 3.2 문서 통째의 위험

- 문서 `original_content` 는 청크 합집합 → 수만~수십만 자 가능. R1 통째 입력 시
  generator 프롬프트가 endpoint context window 초과 가능.
- distractor(§2.2)도 통째 문서 → (d2) `is_answerable` 입력도 초과 가능.
- R3 graph 보강(§4)에서 subgraph_snippet + 문서 원문 합치면 동일 위험.

### 3.3 토큰 카운터 가용성

- **`count_tokens` 존재**: `src/context_loop/processor/chunker.py:94`
  (tiktoken, 없으면 1 char=1 token 폴백 `:108`). build 스크립트에서 import 가능.
- 현재 `build_synthetic_gold_set.py` 는 `count_tokens` 를 import/사용하지 않음.
- LLMClient 인터페이스(`processor/llm_client.py:32,61`)에 입력 한도/컨텍스트
  윈도우 노출 없음 — endpoint 별 한도를 코드에서 알 수 없음(미해결 Q3).

### 3.4 가드 추가 지점(designer 결정 필요)

- 추가 위치 후보:
  - 문서 로더(§1.6) 단계: `max_chars`(또는 신규 `--max-doc-tokens`)로 문서 단위
    1차 필터 — 현 `max_chars` 의미를 청크→문서로 재정의(R1 명시).
  - `_process_chunk_item` 의 `generate_questions` 직전(:687): 토큰/문자 초과 시
    skip+통계 또는 truncate. 통계 키 추가 필요(예 `fail_too_large`,
    stats dict 초기화 `:510-525`).
- 정책(skip vs truncate)은 00_requirements.md:35-37 이 designer 에게 위임.
  기본 동작은 "통째", 극단값만 가드.

---

## 4. R3 — graph 보강 지점 (소유 문서 original_content)

### 4.1 현재 graph 입력 (사실)

- `_process_subgraph_item` (`:839`) 은 `generate_graph_questions(sg, ...)` 호출(:875).
- `generate_graph_questions` (`synth.py:750`) 는 `sg` 에서 `entity_name`,
  `entity_type`, `entity_description`, `edges` 만 사용(:776-781) →
  **subgraph_snippet/edges 만 입력. 소유 문서 원문 미사용.**
- judge 게이트도 `sg["subgraph_snippet"]` 만 입력(`:919-927`).

### 4.2 소유 문서 조회 경로

- `load_candidate_subgraphs` (`:191`) 가 만드는 sg dict 에 이미:
  - `primary_document_id` (:291) — `doc_ids[0]` (`:246`).
  - `document_ids` (:290) — 노드 소유 문서 전체.
- 문서 원문은 `meta_store.get_document(primary_document_id)` (`metadata_store.py:227`,
  `SELECT *`) 또는 `list_documents()` 결과 dict 의 `original_content` 로 조회.
- `load_candidate_subgraphs` 는 이미 `documents = await meta_store.list_documents()`
  (`:220`) 와 `doc_by_id` (`:221`) 를 보유 → **여기서 `original_content` 를 sg dict
  에 함께 실어두면 추가 DB 호출 없이 보강 가능**(예 `sg["primary_document_content"]`).

### 4.3 합치는 지점(designer 결정 필요)

- 입력 합성 후보:
  - `generate_graph_questions` 의 프롬프트(`GRAPH_GENERATE_PROMPT_TEMPLATE`
    `synth.py:161-214`)에 문서 원문 슬롯 추가, 또는
  - sg dict 에 원문을 실어 `_process_subgraph_item` → `generate_graph_questions` 전달.
- **정답/식별 스키마 불변**(00_requirements.md:40): `_make_graph_gold_item`
  (`:1262`) 의 `relevant_doc_ids`/`relevant_doc_groups`/`relevant_graph_entities`
  생성 로직 무변경. 보강은 **generator 입력에만**.
- R2 가드와 일관(00_requirements.md:42): subgraph_snippet + 문서 원문 합산이
  한도 초과 시 동일 truncate/skip 정책. judge 게이트 입력(`sg["subgraph_snippet"]`
  :920)을 보강 원문까지 포함할지는 designer 결정(미해결 Q4).
- cross-doc 모드(`generate_cross_doc_questions` `synth.py:795`)는 R3 범위 밖
  (요구사항이 graph 모드만 명시) — 비침범 유지.

---

## 5. 비침범 확인 (graph/cross_doc 모드 ↔ load_candidate_chunks)

코드로 확정: **graph/cross_doc 모드는 `load_candidate_chunks` 에 비의존, 독립 경로.**

| 모드 | 후보 로더 | distractor 풀 | load_candidate_chunks 참조? |
|------|-----------|---------------|------------------------------|
| chunk | `load_candidate_chunks` :474 | candidates 기반 :491-495 | — (대체 대상) |
| graph | `load_candidate_subgraphs` :979 | subgraphs 기반 :1005-1008 | **없음** |
| cross_doc | `load_cross_doc_seeds` :1197 | seeds snippet 기반 :1211,1225-1227 | **없음** |

- `_run_graph_mode`(:950)는 자체 `load_candidate_subgraphs` + 자체 distractor 풀.
- `_run_cross_doc_mode`(:1174)는 자체 `load_cross_doc_seeds` + 자체 distractor.
- 공유 자원: `extra_korean_stopwords`(build() :499 에서 chunk content 코퍼스로
  학습 후 graph/cross_doc 에도 전달 :574,595) → **R1 으로 코퍼스가 문서 전체로
  바뀌면 graph/cross_doc 게이트의 한국어 stopword 셋도 간접 변화**(미해결 Q1).
  게이트 입력이 아니라 stopword 화이트리스트라 "생성 로직" 변경은 아니지만 결과
  셋이 달라질 수 있음을 designer 가 인지해야 함.
- 그 외 graph/cross_doc 생성 로직은 R3 보강 외 무변경 가능(00_requirements.md:39-42 충족).

---

## 6. R1/R2/R3 충족 판정 + 미해결 질문

### 충족 매트릭스 (현재 HEAD 기준)

| 요구 | 상태 | 근거 |
|------|------|------|
| R1 문서 기반 로더(chunks 비의존) | **미충족** | `load_candidate_chunks` 가 `get_chunks_by_document` :163 로 chunks 테이블 의존. 대체 필요. 단 대체 인프라(`list_documents`+`original_content` `metadata_store.py:233,22`) 이미 존재 |
| R1 doc 단위 distractor 유일성 게이트 | **부분** | 게이트 메커니즘(filter_question d2 `synth.py:984-992`) 그대로 재사용 가능. 풀 분리 키 `chunk_id`(:491) → `document_id` 변경 필요 |
| R1 source_text_anchor 문서 기준 | **부분** | `make_text_anchor`(`synth.py:277`) 문서 원문에 그대로 적용 가능(로직 안전). 호출부 입력만 `content`→`original_content` 교체 |
| R2 입력 한도 가드 | **미충족** | build 경로에 토큰 가드 없음(§3.1). `count_tokens`(`chunker.py:94`) 존재하나 미사용. 정책+구현 필요 |
| R3 graph 소유 문서 원문 보강 | **미충족** | `generate_graph_questions`(`synth.py:776-781`)가 subgraph 만 입력. `primary_document_id`(:291) 로 조회 경로는 확보됨 |
| 하위호환(스키마/포맷 불변) | **충족(영향 없음)** | GoldItem(`gold_set.py:162`) round-trip 무변경. R1/R3 는 생성 입력만 변경, 스키마 미변경 |
| graph/cross_doc 비침범 | **충족(경로 분리 확인)** | §5 — 별도 로더/풀. R3 보강만 graph 입력에 영향 |
| 결정성(문서 단위 seed) | **부분** | seed 키 `chunk_index`(:682) → `document_id` 재정의하면 유지 가능 |

### 미해결 질문 (designer 에게)

- **Q1 — 한글 stopword 코퍼스 변화**: `build_korean_stopwords_from_corpus`
  (build() :499) 입력이 청크 content → 문서 original_content 전체로 바뀌면
  빈도 분포/메모리(`max_stopwords=500`)가 달라져 게이트 결과가 변할 수 있음.
  코퍼스를 문서 전체로 둘지, threshold 재조정할지?
- **Q2 — source_section_path 대체**: 문서 모드엔 청크 섹션 경로 없음(:723,762).
  빈 문자열로 둘지, 문서 title/url 로 대체할지? (디버그 필드, 채점 무관)
- **Q3 — endpoint 입력 한도 미상**: LLMClient(`llm_client.py`)가 context window 를
  노출 안 함. R2 가드 한도값을 CLI 옵션(`--max-doc-tokens`)으로 받을지, 하드코딩
  기본값(예 16k/32k 토큰)으로 둘지? truncate vs skip 기본값은?
- **Q4 — R3 judge 게이트 입력 범위**: graph 보강 원문을 generator 입력에만 넣을지,
  judge `filter_question`(:919)의 source 입력(현 `subgraph_snippet`)에도 합칠지?
  judge 입력까지 합치면 (a)`is_answerable`/(d1)`is_unique_source` 의미가 바뀜.
- **Q5 — distractor 통째 문서 비용**: doc distractor 가 통째라 (d2) judge 호출
  입력이 큼(§2.2). distractor 도 anchor/prefix 로 잘라 넣을지(게이트 신뢰도
  vs 토큰 비용 트레이드오프)? R2 가드를 distractor 에도 적용?

### 영향받는 테스트 (마이그레이션 필요)

- `tests/test_eval/test_build_synthetic_gold_set.py:46-118` —
  `test_load_candidate_chunks_*` 2건: 함수 대체 시 **재작성/대체** 필요(문서 기반
  로더용 신규 테스트).
- `tests/test_eval/test_concurrency.py:122-187` — `_process_chunk_item` 테스트가
  chunk dict(`chunk_index`/`content`/`section_path` 키, `:126-134`,`:168-172`)를
  직접 구성. dict 키 스키마 변경 시 **수정** 필요.
- `tests/test_eval/test_concurrency.py:234-` `_run_chunk_mode` 테스트도 chunk dict
  사용 — 키 변경 영향.
- `tests/test_eval/test_synth.py:468-490` `make_text_anchor` 테스트 — **영향 없음**
  (함수 시그니처/로직 불변).
- graph/cross_doc 테스트(`test_build_synthetic_gold_set.py:126+`,
  `test_concurrency.py:189+`): R3 보강이 sg dict 에 키를 **추가**만 하면 기존 테스트
  무영향. 단 `generate_graph_questions` 프롬프트 변경 시 관련 테스트 확인.
