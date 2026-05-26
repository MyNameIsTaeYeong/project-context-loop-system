# 02_design — 문서 기반 골드셋 생성 전환 설계 (R1/R2/R3)

작성: 2026-05-22 (designer, eval-gold-set-improvement)
기준: 현재 HEAD (PR#65 stack, `relevant_doc_groups` 반영)
입력: `00_requirements.md` (대체/통째/그래프 보강), `01_analysis.md` (영향 분석)
원칙: implementer가 이 문서만 보고 구현 가능하도록 파일:라인 + 함수 시그니처 + dict 키 단위로 명시.
**코드 직접 수정 없음 — 설계만.**

> 변경 이력: 이전 02_design(PR#65 cross-doc/answer-equivalence)을 본 작업(문서 기반 전환) 스코프로 전면 교체.

---

## 0. 설계 목표 (요구사항 → 결정 매핑)

| 요구 | 설계 결정 | 섹션 |
|------|-----------|------|
| R1 문서 기반 로더 (chunks 비의존) | D1 `load_candidate_documents` 신설, `load_candidate_chunks` 제거 | §1 |
| R1 doc 단위 distractor 게이트 | D2 distractor 풀 분리 키 `chunk_id`→`document_id`, distractor 본문 prefix 절단 | §2 |
| R2 입력 한도 가드 | D3 `count_tokens` 기반 한도 가드 + `--max-doc-tokens` CLI + skip/truncate 정책 | §3 |
| R3 graph 소유 문서 원문 보강 | D4 `load_candidate_subgraphs`의 `doc_by_id` 재활용 → sg dict에 원문 적재 → generator 프롬프트 보강 | §4 |
| 하위 호환 (스키마 불변) | D5 GoldItem 무변경, CLI `--n-chunks` 의미만 재정의 | §5 |
| 미해결 Q1~Q5 | D6 각 결정 | §6 |

추적성 원칙: 모든 변경은 **생성 입력**에만 영향. **정답/채점 스키마(GoldItem, relevant_doc_*)는 일절 변경 없음.**

---

## 1. R1 — `load_candidate_documents` 신설 (chunk 로더 대체)

### 1.1 신 함수 시그니처

`scripts/build_synthetic_gold_set.py` 의 `load_candidate_chunks`(현 :132-183)를
**삭제하고** 아래 함수로 대체한다. 같은 위치(§ "Chunk loading" 헤더 → "Document loading" 으로 변경)에 둔다.

```python
async def load_candidate_documents(
    store: MetadataStore,
    *,
    source_types: list[str] | None,
    min_chars: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """metadata_store 에서 문서 후보를 로드한다 (R1 — chunks 테이블 비의존).

    각 항목 dict 형태::

        {
            "document_id": int,          # 정답 doc + 결정성 seed 키
            "source_type": str,          # stratified_sample / distractor 필터 키
            "content": str,              # original_content (Generator 입력 = 통째)
            "title": str,                # 디버그 메타 (source_section_path 대체용)
            "url": str,                  # 디버그 메타
        }

    original_content 길이(문자) 기준으로 min/max_chars 필터. NULL/빈 문서 제외.
    """
```

### 1.2 구현 지시 (라인 단위)

`load_candidate_chunks` 본문(:156-183)을 아래로 대체:

```python
documents = await store.list_documents()          # metadata_store.py:233, SELECT *

out: list[dict[str, Any]] = []
for doc in documents:
    if source_types and doc.get("source_type") not in source_types:
        continue
    content: str = doc.get("original_content") or ""   # NULL 가드 (metadata_store.py:22 nullable)
    if not content.strip():
        continue
    if len(content) < min_chars or len(content) > max_chars:
        continue
    out.append({
        "document_id": doc["id"],
        "source_type": doc.get("source_type", ""),
        "content": content,
        "title": doc.get("title") or "",
        "url": doc.get("url") or "",
    })
# 결정론적 순서 — document_id 오름차순 (1 doc = 1 후보, chunk_index 불필요)
out.sort(key=lambda x: x["document_id"])
logger.info("후보 문서 로드 완료 — total=%d", len(out))
return out
```

근거(analyst §1.6): `list_documents`가 `original_content` 포함 전 컬럼 반환 → `get_chunks_by_document` 제거로 chunks 테이블 완전 비의존.

### 1.3 dict 키 매핑 — `content` 키 유지 결정 (중요)

analyst가 "content→original_content" 매핑을 제안했으나, **dict 키 이름은 `content`로 유지**한다 (값만 문서 원문으로 채움).

- **이유**: `_process_chunk_item`이 `chunk["content"]`를 3곳(:687 generate, :712 anchor, :738 distractor 본문)에서 사용. distractor 풀의 `[d["content"] ...]`(:738)도 동일. 키를 `original_content`로 바꾸면 5개 이상 호출부를 일괄 수정해야 하고 `stratified_sample`/distractor 코드의 의미도 흐려진다.
- **트레이드오프**: dict 키 `content`가 "청크 본문"이 아닌 "문서 원문"을 담게 됨 — docstring으로 명시(위 §1.1)하면 혼선 없음. 채점 키가 아니므로 안전.
- **단, seed/풀 분리 키는 반드시 변경** (§1.4, §2.1). 이 키들은 의미가 실제로 바뀌기 때문.

따라서 `_process_chunk_item`(:650-770)의 `chunk["content"]` 사용부는 **무변경**. 함수/변수명만 가독성을 위해 정리(선택, 아래 §1.7).

### 1.4 deterministic seed — `chunk_index` → `document_id`

`_process_chunk_item` :681-686 의 seed 계산을 변경:

```python
# 변경 전 (:682)
item_seed = generator_seed_base + int(chunk.get("chunk_index") or 0) ...
# 변경 후
item_seed = (
    generator_seed_base + int(chunk["document_id"])
    if generator_seed_base is not None
    else None
)
```

- 문서당 1회 처리 → `document_id`가 충돌 없는 안정 seed (analyst §1.3, 00_req:49 결정성 충족).
- `judge_seed = item_seed + 10000 + j`(:732)는 무변경.
- 로그(:673-677)의 `chunk_index=%d` 포맷은 `document_id`만 남기도록 수정:
  `"[doc start %d/%d] doc=%d, source_type=%s"` 로 변경, `chunk["chunk_index"]` 인자 제거.

### 1.5 `section_path` 소실 처리 (analyst Q2 → D6-2)

`_process_chunk_item`의 `source_section_path=chunk["section_path"]`(:723, :762)에서
문서 dict에는 `section_path`가 없다(청크 단위 개념).

**결정 (D6-2)**: `source_section_path`에 **문서 `title`을 넣는다**.

```python
source_section_path=chunk["title"],   # :723, :762 (문서 모드 — 청크 섹션 경로 없음, title 로 대체)
```

- **이유**: `source_section_path`는 디버그 전용 필드(채점 무관, GoldItem `gold_set.py:194` str 기본 ""). 빈 문자열보다 문서 제목이 디버깅 시 출처 식별에 유용. URL은 confluence엔 있으나 git_code/upload엔 없을 수 있어 title이 더 일관적.
- GoldItem `to_dict`(`gold_set.py:226`)는 빈 문자열이면 직렬화 생략 → title이 있으면 기록, 없으면 생략. 스키마 불변.

### 1.6 source_text_anchor — 문서 원문 기준 (R1 충족)

`anchor = make_text_anchor(chunk["content"])`(:712) **무변경**.
- `make_text_anchor`(`synth.py:277`)는 임의 텍스트의 whitespace 정규화 후 앞 200자(`ANCHOR_MAX_CHARS`). 문서 원문에 그대로 적용 안전(analyst §1.5, 테스트 `test_synth.py:468-490` 불변).
- 의미: anchor가 "문서 도입부 200자" → 추적성 유지(00_req:32). 채점 키 아니므로 기능 영향 없음.
- **R2 truncate와의 순서**: anchor는 truncate 이전 원본(`chunk["content"]`) 기준 — §3.3 참조.

### 1.7 함수/변수 리네이밍 (선택, 권장)

가독성을 위해 `_process_chunk_item`/`_run_chunk_mode`를 `_process_document_item`/`_run_document_mode`로, 내부 변수 `chunk`→`doc`로 리네임 **권장**하되 **필수 아님**.
- **필수**: 호출부(:532 `_run_chunk_mode(...)`, :797 `_process_chunk_item(...)`)와 정의부 일관 유지.
- **테스트 영향**: `test_concurrency.py:122-187`가 `_process_chunk_item`을 직접 import/호출 → 리네임 시 테스트도 수정(§7). 리네임을 안 하면 테스트 import는 그대로 유지되어 변경 최소화. **implementer 판단: 리네임 권장하되, 미리네임 시 docstring으로 "문서 1건 처리"임을 명시.** 본 설계의 나머지 라인 참조는 기존 이름 기준으로 작성.
- stats 키 `"generated"/"passed"` 등은 무변경(모드 무관 공용).

---

## 2. R1 — distractor 게이트를 문서 단위로

### 2.1 distractor 풀 분리 키 (`build()` :491-495)

```python
# 변경 전
sampled_chunk_ids = {s["chunk_id"] for s in sampled}                 # :491
distractor_pool = [c for c in candidates if c["chunk_id"] not in sampled_chunk_ids]
# 변경 후
sampled_doc_ids = {s["document_id"] for s in sampled}
distractor_pool = [
    c for c in candidates if c["document_id"] not in sampled_doc_ids
]
rng.shuffle(distractor_pool)   # :495 무변경
```

- **효과**: distractor = "샘플되지 않은 다른 문서". 같은 문서가 distractor가 될 수 없음 → 게이트 의미가 "다른 문서로도 답 가능하면 generic"으로 정합(analyst §2.2, doc 단위 채점과 일치). 기존 chunk 모드의 "자기 문서 옆 청크가 distractor가 되던 약점"(analyst §2.2) 제거.

### 2.2 distractor 본문 비용 완화 — prefix 절단 (analyst Q5 → D6-5)

distractor가 통째 문서가 되면 (d2) `is_answerable` 입력 토큰이 급증(analyst §2.2, §1.4).

**결정 (D6-5)**: distractor는 **원문 전체가 아니라 앞부분 prefix만** generic 게이트에 넣는다.

`_process_chunk_item` :735-743 의 filter_question 호출에서 distractor 본문 소스를 변경:

```python
# 변경 전 (:738)
[d["content"] for d in same_type_distractors],
# 변경 후
[_distractor_excerpt(d["content"]) for d in same_type_distractors],
```

신규 모듈 헬퍼 (`build_synthetic_gold_set.py` 상단, 상수 정의부 근처):

```python
# distractor 본문은 generic 게이트(is_answerable)에 입력되므로, 통째 문서를
# 넣으면 judge 토큰이 폭주한다(R2 일관). 앞부분 prefix 만으로도 "다른 문서로
# 답이 되는가" 판정에 충분 — 토큰 비용을 상수로 고정한다.
DISTRACTOR_EXCERPT_CHARS = 2000

def _distractor_excerpt(content: str) -> str:
    """distractor 문서 본문의 앞부분만 잘라 generic 게이트 비용을 제한한다."""
    return content[:DISTRACTOR_EXCERPT_CHARS]
```

- **이유**: (d2) 게이트는 "무관 문서로도 답이 되면 generic 탈락"을 판정. 문서 도입부 2000자(≈500~700토큰)면 주제 식별에 충분하고, distractor당 입력이 상수로 고정되어 `n_distractors`(기본 2) × 큰 문서 폭주를 방지.
- **트레이드오프**: 답 근거가 distractor 문서 후반부에만 있는 드문 경우 generic 탈락을 놓칠 수 있음 → 게이트 신뢰도 미세 하락. 토큰 비용/안정성 우위로 수용. 정답 source는 통째 입력하므로 답변 가능성 판정의 정확도는 유지.
- `DISTRACTOR_EXCERPT_CHARS`는 상수(CLI 노출 안 함 — YAGNI). 문자 기준 prefix 절단(토큰 카운트 불필요 — 게이트 비용 상한이 목적).

### 2.3 anchor/distractor와 게이트의 관계 (재확인)

`make_text_anchor`는 게이트에 들어가지 않음(GoldItem 필드 전용, analyst §2.3). distractor만 게이트 입력 — §2.2가 비용 핵심.

---

## 3. R2 — generator 입력 한도 가드

### 3.1 정책 결정 (analyst Q3 → D6-3)

기본 동작은 "통째". 한도 초과 문서만 가드한다.

**결정**:
- 한도값: 신규 CLI `--max-doc-tokens` (기본 `24000`). 0 이면 가드 비활성(무제한).
- 초과 처리: **truncate (앞부분)**. skip 아님.
- 통계: `count_tokens` 기반 truncate 발생 건수를 stats에 기록.

**근거**:
- truncate vs skip: "통째" 의도(00_req:17)에 가장 가깝게 — 문서를 버리지 않고 앞부분으로라도 질문 생성. skip하면 큰 문서가 골드셋에서 통째 누락되어 커버리지 손실. 문서 도입부/개요가 보통 핵심을 담으므로 앞부분 truncate가 합리적.
- 24000 토큰: LLMClient가 context window를 노출하지 않음(analyst §3.3) → 보수적 기본값. 대부분 endpoint(32k+)에서 출력(`max_tokens=1024`) + 프롬프트 여유 확보. 환경별로 CLI로 조정 가능.

### 3.2 구현 — truncate 헬퍼 (`synth.py`에 정의)

`count_tokens`를 사용하는 truncate 헬퍼를 **`synth.py`에 public 함수로 신설**한다 (chunk/graph 양쪽 공유, §4.2와 일관). `make_text_anchor`(`synth.py:277`) 근처에 배치.

```python
from context_loop.processor.chunker import count_tokens  # synth.py 상단 import (순환 없음)

def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """text 가 max_tokens 초과면 앞부분으로 truncate. (잘린 텍스트, truncated?) 반환.

    max_tokens<=0 이면 가드 비활성 — 원본 그대로 반환.
    tiktoken 부재 시 count_tokens 는 1char=1token 폴백이므로 보수적으로 동작.
    """
    if max_tokens <= 0:
        return text, False
    if count_tokens(text) <= max_tokens:
        return text, False
    # 토큰→문자 환산이 비결정적이므로 비례 1차 절단 후 초과분만 추가 축소(결정론).
    approx_chars = max_tokens * 3  # cl100k 한국어/코드 혼합 보수적 환산
    cut = text[:approx_chars]
    while count_tokens(cut) > max_tokens and len(cut) > 100:
        cut = cut[: int(len(cut) * 0.9)]
    return cut, True
```

- 결정론 유지(00_req:49): 같은 입력 → 같은 절단 결과.
- build 스크립트는 `from context_loop.eval.synth import truncate_to_tokens` 로 사용.

### 3.3 가드 적용 지점 (chunk 모드 — 단일 지점)

`_process_chunk_item`의 `generate_questions` **직전**(:687)에 적용:

```python
gen_content, truncated = truncate_to_tokens(chunk["content"], max_doc_tokens)
if truncated:
    local_stats["truncated_too_large"] = local_stats.get("truncated_too_large", 0) + 1
generated = await generate_questions(gen_content, n=..., ...)   # :687 (인자만 chunk["content"]→gen_content)
...
anchor = make_text_anchor(chunk["content"])   # :712 — 원문(truncate 전) 기준, 추적성 보존
```

- **anchor는 원문(`chunk["content"]`) 기준 유지** — truncate는 generator 입력에만. anchor는 항상 도입부 200자라 truncate 무관.
- `max_doc_tokens`를 시그니처에 **신규 파라미터로 전파**:
  - `_process_chunk_item(...)`(:650): `max_doc_tokens: int = 0` (kwonly) 추가.
  - `_run_chunk_mode(...)`(:773): `max_doc_tokens: int = 0` 추가, :797 `_process_chunk_item(...)` 호출에 전달.
  - `build()`(:393): `max_doc_tokens: int = 0` 추가, :532 `_run_chunk_mode(...)` 호출에 전달.

### 3.4 stats 키 등록

`build()` :510-525 stats dict 초기화에 추가:

```python
"truncated_too_large": 0,
```

`_merge_stats`(:833)는 동적 키 합산이므로 자동 반영. metadata.stats(:613)에 노출되어 사후 추적 가능.

### 3.5 문서 로더 단계의 max_chars와의 관계

- `--max-chars`(§5에서 기본값 상향)는 **문자 단위 1차 필터**(load_candidate_documents §1.2, "후보 제외"). `--max-doc-tokens`는 **토큰 단위 2차 가드**(generate 직전 truncate, "입력 자르기"). 둘은 독립.
- max_chars를 크게 두고 토큰 가드로 truncate하는 것이 "통째" 정책에 부합 → §5에서 max_chars 기본값 상향.

---

## 4. R3 — graph 질의 소유 문서 원문 보강

### 4.1 sg dict에 원문 적재 (추가 DB 호출 없음)

`load_candidate_subgraphs`(:191)는 이미 `doc_by_id`(:221)를 보유.
sg dict 생성부(:286-295)에 키 1개 추가:

```python
out.append({
    "entity_name": name,
    ...
    "subgraph_snippet": snippet,
    "primary_document_content": (                       # R3 — 소유 문서 원문 (추가 DB 호출 없음)
        doc_by_id.get(primary_doc_id, {}).get("original_content") or ""
    ),
})
```

- `doc_by_id`는 `list_documents()`(:220, SELECT *)에서 이미 만들어짐 → `original_content` 즉시 사용 가능(analyst §4.2). 추가 쿼리 없음.

### 4.2 generator 프롬프트에 원문 슬롯 추가

`generate_graph_questions`(`synth.py:750`)에서 보강 원문을 프롬프트에 합친다.

`GRAPH_GENERATE_PROMPT_TEMPLATE`(`synth.py:161-214`)에 **소유 문서 발췌 슬롯** 추가.
"주변 관계:" 블록(:167-168) 다음, "이 엔티티 또는 관계에서..."(:170) 앞에 삽입:

```
주변 관계:
{edges_text}

소유 문서 발췌(참고용 — 질문 표현 다양화/정확도 향상):
{document_excerpt}

이 엔티티 또는 관계에서 답을 찾을 수 있는, ...
```

`generate_graph_questions` :776-782 의 format 호출에 슬롯 채움:

```python
doc_excerpt = subgraph.get("primary_document_content", "") or "(문서 원문 없음)"
doc_excerpt, _ = truncate_to_tokens(doc_excerpt, doc_max_tokens)   # R2 일관 (§3.2)
prompt = GRAPH_GENERATE_PROMPT_TEMPLATE.format(
    entity_name=...,
    entity_type=...,
    entity_description=...,
    edges_text=format_edges_for_prompt(...),
    document_excerpt=doc_excerpt,
    n=n,
)
```

- truncate 헬퍼는 §3.2의 `truncate_to_tokens`(synth.py 내) 재사용 — chunk/graph 동일 헬퍼 → R2 일관(00_req:42).

### 4.3 한도 가드 전파 (R2 일관)

`generate_graph_questions`(`synth.py:750`)에 신규 kwonly 파라미터 `doc_max_tokens: int = 0` 추가:

```python
async def generate_graph_questions(
    subgraph, *, n, generator,
    reasoning_mode="off", max_tokens=1024, temperature=0.0, seed=None,
    doc_max_tokens: int = 0,          # NEW (R2/R3 — 보강 원문 truncate 한도)
) -> list[GeneratedGraphQuestion]:
```

- `_process_subgraph_item`(:875)에서 `generate_graph_questions(sg, ..., doc_max_tokens=max_doc_tokens)` 전달.
- `max_doc_tokens`를 `_process_subgraph_item`(:839)/`_run_graph_mode`(:950) 시그니처에 `max_doc_tokens: int = 0` 추가. `build()` :552 `_run_graph_mode(...)` 호출에서 chunk 모드와 **동일 값** 전달.

### 4.4 judge 게이트 입력 범위 (analyst Q4 → D6-4)

**결정 (D6-4)**: 보강 원문을 **generator 입력에만 넣고, judge 게이트(`filter_question` :919-927)는 기존 `sg["subgraph_snippet"]` 그대로 유지**한다.

`_process_subgraph_item` :919-927 의 filter_question 호출 **무변경**:
```python
report = await filter_question(
    gq.query,
    sg["subgraph_snippet"],          # 무변경 — 원문 합치지 않음
    distractor_snippets,
    ...
)
```

- **이유 (00_req:40 "보강은 생성 입력에만 영향")**: judge의 (a)`is_answerable`/(d1)`is_unique_source`는 "그래프 컨텍스트로 답이 되는가"를 판정. 여기에 통째 원문을 합치면 게이트 의미가 "문서로 답이 되는가"로 변질되어 graph 항목 정체성이 흐려진다. **graph 골드 항목(그래프로 답)을 보존**하려면 judge는 subgraph_snippet 기준이어야 함.
- **트레이드오프**: generator가 원문 보고 만든 질문을 judge가 snippet만으로 검증 → 일부 (a) is_answerable 탈락 가능. 이는 **의도된 동작** — snippet으로 답 안 되는 질문은 graph 평가에 부적합. 보강의 역할은 "질문 표현 다양화/자연스러움"이지 "답 범위 확장"이 아님.

### 4.5 cross_doc 비침범

`generate_cross_doc_questions`(`synth.py:795`) 및 cross_doc 경로는 **무변경**(R3 범위 밖, 00_req:39, analyst §5). graph 모드만 보강.

---

## 5. 마이그레이션 / CLI

### 5.1 GoldItem 스키마 — 불변 (D5)

- `GoldItem`(`gold_set.py:163`) 필드/`to_dict`/`from_dict` **일절 변경 없음**. `relevant_doc_ids=[document_id]`, `source_document_id`, `source_text_anchor`, `source_section_path` 모두 기존 필드에 그대로 채움. 기존 YAML 골드셋 round-trip 무손실(00_req:45).
- chunk(문서) 모드는 단일 문서 → `relevant_doc_groups` 빈 리스트 유지(현 동작과 동일. graph/cross_doc만 :1314/:1343에서 설정).

### 5.2 CLI 변경

| 인자 | 변경 | 내용 |
|------|------|------|
| `--n-chunks` | **의미 재정의 (이름 유지)** | "샘플링할 청크 수" → "샘플링할 **문서** 수". help 텍스트 수정(:1492-1493). 이름은 하위호환 위해 유지(스크립트/CI에서 사용 중일 수 있음). 변수 `n_chunks`(build :396, :484 stratified n_total)는 그대로. 모듈 docstring 사용법(:19, :75) 문구 갱신. |
| `--min-chars` | **의미 재정의** | "최소 청크 길이" → "최소 **문서** 길이"(문자). 기본 200 유지. help 수정(:1531-1532). |
| `--max-chars` | **의미 재정의 + 기본값 상향** | "최대 청크 길이" → "최대 **문서** 길이"(문자) 1차 필터. 기본 `8000` → **`200000`** 으로 상향(통째 정책 — 큰 문서를 후보에서 버리지 않음. 토큰 가드가 §3에서 truncate). help 수정(:1535-1536). |
| `--max-doc-tokens` | **신규** | generator 입력 토큰 한도. 기본 `24000`. 0=무제한. 초과 시 앞부분 truncate(§3). help: "generator 입력 문서 토큰 한도. 초과분은 앞부분 truncate. 0=무제한(기본 24000)." `args.max_doc_tokens` → build() 전달(:1739 호출에 `max_doc_tokens=args.max_doc_tokens` 추가). |
| `--n-distractors` | 무변경 | distractor "문서" 수로 의미만 변(help "무관 청크"→"무관 문서" 미세 수정, :1528). |
| `--n-graph-nodes`, `--source-types`, `--seed`, `--n-gold-sets` 등 | 무변경 | §1.2에서 source_type 키 보존하므로 `--source-types` 그대로 작동. |

### 5.3 build() / 호출부 plumbing

- `build()`(:393): `max_doc_tokens: int = 0` 파라미터 추가(§3.3). docstring에 의미 추가.
- `main()` :1739 build() 호출에 `max_doc_tokens=args.max_doc_tokens` 추가.
- `build()` :474 `load_candidate_chunks(...)` → `load_candidate_documents(...)` 호출명 변경(시그니처 동일).
- :480-481 빈 후보 메시지 "후보 청크가 없습니다" → "후보 문서가 없습니다".
- :487, :606 로그/metadata 문구는 그대로 둬도 무방(metadata 키 하위호환). :598 `generation_modes = ["chunk"]`는 그대로 — 모드 식별자 "chunk"는 골드셋 metadata 호환 위해 유지(이름만, 내부는 문서 기반).

### 5.4 한글 stopword 코퍼스 (analyst Q1 → D6-1)

`build()` :499-503 `build_korean_stopwords_from_corpus([c["content"] ...])`:
- `c["content"]`가 이제 문서 원문(통째) → 코퍼스가 커짐.

**결정 (D6-1)**: 코퍼스 입력은 **문서 원문 그대로 두되, `min_corpus_freq`를 5→8로 상향**.
- **이유**: 문서 원문은 청크 합집합이라 같은 어휘 빈도가 자연 증가 → freq=5 임계가 너무 느슨해져 stopword가 과다 학습(고유명사까지 일반어로 오분류)될 위험. 임계 8로 청크 시절과 유사한 선별 강도 유지. `max_stopwords=500` 캡 유지.
- 변경: `build()` :501 `min_corpus_freq=5` → `min_corpus_freq=8`. (`build_korean_stopwords_from_corpus` 함수 자체는 무변경.)
- 영향: graph/cross_doc 게이트도 이 stopword 셋 공유(:574,595) → 일관 적용. 게이트 결과 미세 변동 가능하나 "생성 로직" 변경 아님(화이트리스트 조정).

---

## 6. 미해결 질문 5건 — 설계 결정

| # | analyst 질문 | 결정 | 사용자 결정 필요? |
|---|--------------|------|:---:|
| **D6-1 (Q1)** | stopword 코퍼스 문서 전체로 변경 | 코퍼스는 문서 원문 유지, `min_corpus_freq` 5→8 상향 (§5.4) | 아니오 |
| **D6-2 (Q2)** | section_path 대체 | `source_section_path`에 문서 `title` 사용 (§1.5) | 아니오 |
| **D6-3 (Q3)** | 입력 한도값·정책 | `--max-doc-tokens` 기본 24000, 초과 시 **truncate**(skip 아님), `count_tokens` 사용 (§3) | **예** |
| **D6-4 (Q4)** | R3 judge 게이트 범위 | 보강 원문은 **generator 입력에만**, judge는 subgraph_snippet 유지 (§4.4) | 아니오 |
| **D6-5 (Q5)** | distractor 통째 비용 | distractor는 **앞부분 2000자 prefix**만 게이트 입력 (§2.2) | 아니오 |

**사용자 결정 권장 항목**: D6-3의 `--max-doc-tokens` 기본값(24000)과 truncate-vs-skip 정책. 사내 generator endpoint의 실제 context window를 모르므로(analyst §3.3) 보수적 기본값을 두었으나, 운영 endpoint가 더 작으면(예: 8k) 조정 필요. **기본값으로 진행 가능하되 빌드 전 endpoint 한도 확인 권장.** 나머지(D6-1/2/4/5)는 designer 기본값으로 확정.

---

## 7. 테스트 전략

| 테스트 | 변경 종류 | 내용 |
|--------|-----------|------|
| `test_build_synthetic_gold_set.py:46-118` `test_load_candidate_chunks_*` (2건) | **재작성** | `test_load_candidate_documents_*`로 대체. 시나리오: (a) `original_content` 기반 후보 로드, chunks 테이블 미조회(mock store에 chunks 없어도 동작), (b) min/max_chars 필터(문자), (c) NULL/빈 original_content 제외, (d) source_type 필터, (e) document_id 오름차순 정렬. |
| `test_concurrency.py:122-187` `_process_chunk_item` | **수정** | 입력 dict 키를 문서 dict 스키마(`document_id`/`source_type`/`content`/`title`)로 변경. `chunk_index`/`section_path` 키 제거. seed가 `document_id` 기반인지 검증(결정성). `source_section_path==title` 검증. |
| `test_concurrency.py:234-` `_run_chunk_mode` | **수정** | 동일 dict 키 변경. distractor 풀 분리가 `document_id` 기반인지 검증. |
| **신규** distractor 게이트 | **추가** | distractor 풀에서 정답 문서가 제외되는지(`document_id` 기준), distractor 본문이 `DISTRACTOR_EXCERPT_CHARS`로 잘려 게이트에 들어가는지(`_distractor_excerpt` 단위 테스트). |
| **신규** R2 한도 가드 | **추가** | `truncate_to_tokens`: (a) max<=0 → 무변경+False, (b) 한도 이하 → 무변경+False, (c) 초과 → 잘린 텍스트+True + `count_tokens(결과)<=max`. `_process_chunk_item`에서 truncate 시 `truncated_too_large` stats 증가 검증. anchor는 truncate 전 원문 기준 검증. |
| **신규** R3 graph 보강 | **추가** | `load_candidate_subgraphs` 산출 sg dict에 `primary_document_content` 키 존재(추가 DB 호출 없이 doc_by_id에서). `generate_graph_questions`가 프롬프트에 `document_excerpt` 슬롯을 채우는지(generator mock으로 프롬프트 캡처). judge 입력은 여전히 `subgraph_snippet`인지(보강 원문 미포함) 검증. |
| `test_synth.py:468-490` `make_text_anchor` | **무변경** | 함수 불변. |
| graph/cross_doc 기존 테스트 | **무변경 확인** | sg dict에 키 **추가**만 → 기존 assert 무영향. `GRAPH_GENERATE_PROMPT_TEMPLATE` 정확 문자열 비교 테스트가 있으면 슬롯 추가 반영해 갱신(grep 확인). |

- **LLM 호출은 모두 mock** (`LLMClient.complete` mock — 도메인 규칙). 실제 API 호출 없음.
- pytest + pytest-asyncio, ruff 통과. PR#65 선재 실패 5건은 건드리지 않음(00_req:51).
- **재현성 회귀**: 같은 seed + 같은 mock store → 같은 골드셋(특히 `document_id` 기반 seed, 00_req:49).

---

## 8. 변경 파일 목록

| 파일 | 변경 종류 | 핵심 변경 |
|------|-----------|-----------|
| `scripts/build_synthetic_gold_set.py` | 수정 | `load_candidate_chunks`→`load_candidate_documents`(§1.1-1.2). distractor 풀 키 `chunk_id`→`document_id`(§2.1). `DISTRACTOR_EXCERPT_CHARS`+`_distractor_excerpt`+게이트 적용(§2.2). seed `chunk_index`→`document_id`(§1.4). `source_section_path`=title(§1.5). R2 가드 적용(§3.3)+stats 키(§3.4). sg dict에 `primary_document_content`(§4.1). `max_doc_tokens` plumbing(build/_run_chunk_mode/_process_chunk_item/_run_graph_mode/_process_subgraph_item). CLI: `--max-doc-tokens` 신규, `--n-chunks`/`--min-chars`/`--max-chars` help·기본값(§5.2). stopword `min_corpus_freq` 8(§5.4). `truncate_to_tokens` import. |
| `src/context_loop/eval/synth.py` | 수정 | `count_tokens` import + `truncate_to_tokens` 헬퍼 신설(public, :277 근처, §3.2). `GRAPH_GENERATE_PROMPT_TEMPLATE`에 `{document_excerpt}` 슬롯 추가(§4.2). `generate_graph_questions`에 `doc_max_tokens` 파라미터 + 원문 슬롯 채움 + truncate(§4.2-4.3). |
| `src/context_loop/eval/gold_set.py` | **무변경** | 스키마 불변(D5). |
| `src/context_loop/processor/chunker.py` | **무변경** | `count_tokens` 재사용만. |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정 | 로더 테스트 재작성(§7). |
| `tests/test_eval/test_concurrency.py` | 수정 | dict 키·seed·distractor 테스트 수정(§7). |
| `tests/test_eval/test_synth.py` | 추가 | `truncate_to_tokens` + graph 보강 테스트(§7). |

---

## 9. 위험 / 미해결 (구현 중 결정)

1. **`--max-doc-tokens` 기본값(24000)** — 사내 generator endpoint context window 미상(analyst §3.3). 작으면 truncate 빈발 → stats `truncated_too_large`로 사후 확인. **사용자 결정 권장**(§6 D6-3).
2. **함수 리네임 여부**(`_process_chunk_item`→`_process_document_item`) — implementer 재량. 리네임 시 테스트 import 동반 수정(§1.7). 본 설계는 미리네임 기준 라인 참조.
3. **`truncate_to_tokens` 토큰→문자 환산**(§3.2) — `approx_chars=max_tokens*3`은 보수치. while 루프로 수렴 보장.
4. **`GRAPH_GENERATE_PROMPT_TEMPLATE` 프롬프트 비교 테스트** — 슬롯 추가로 정확 문자열 매칭 테스트가 깨질 수 있음. implementer가 grep 확인 후 갱신.
5. **stopword `min_corpus_freq` 8**(§5.4) — 경험적 값. 골드셋 품질로 사후 조정 가능.

---

**완료**: 변경 파일 코드 2개(+무변경 2개) + 테스트 3개, 권장 테스트 7+개. 다음 단계 → implementer.
