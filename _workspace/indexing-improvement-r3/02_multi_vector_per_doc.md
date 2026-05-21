# R3 보강 — 문서 단위 반환 + 멀티 벡터/가상 질문 인덱싱

## 사용자 아이디어

> 리턴은 문서 단위로 하지만 검색 임베딩은 여러 청크에서 질문을 추출해서 넣어놓기

이는 RAG 업계에서 검증된 패턴 — 여러 이름이 있다:
- **Multi-vector retrieval** (LangChain 의 MultiVectorRetriever)
- **Proposition indexing** (Anthropic "contextual retrieval" 보고)
- **Question-based / HyDE indexing** (검색 시 HyDE 가 아니라 인덱싱 시 가상 질문 생성)

## 왜 정밀도가 올라가나

사용자 query 는 자연어 질문 형태("AuthService 는 어떻게 토큰을 검증하나?").
임베딩 key 가 본문(설명문)이면 query 와 형태가 달라 cosine 거리가 멀어진다.
임베딩 key 도 질문 형태면 같은 의미 공간에서 거리가 가까워진다.

추가로, 한 문서가 다루는 여러 측면을 **각각 별도 벡터**로 등록하면 부분 매칭 정밀도가 그대로 유지된다 — "관련 문서 찾기" 유즈케이스에서 큰 문서가 자기 문서의 핵심을 다 표현하지 못해 누락되는 문제를 해소.

## 옵션 D 변형 3가지

### D-1: 멀티 벡터 (가상 질문 없음)

- 문서 1개당 N개 벡터: **문서 전체 임베딩 1** + **섹션별 임베딩 M**
- query 임베딩과 cosine 매칭 후 `document_id` 단위로 dedup → 문서 단위 반환
- 매칭된 섹션을 출처 라벨로 보여줌 (현재 `section_path` 그대로 활용)

| 측면 | 영향 |
|------|------|
| 인덱싱 비용 | 옵션 D 대비 +10~30% (섹션별 임베딩) |
| 검색 정밀도 | 옵션 D 보다 ↑ (부분 매칭 복원) |
| LLM 추가 호출 | **0** (가상 질문 없음) |
| 출처 라벨 | 매칭 섹션 표시 — UX 무변경 |
| 구현 난이도 | ★ (지금 멀티뷰 dedup 메커니즘 그대로 확장) |

### D-2: 가상 질문 임베딩 (HyDE 인덱싱) — 사용자 아이디어 직역

- 문서 1개당: **문서 전체 임베딩 1** + 섹션별 LLM 호출로 **"이 본문이 답할 수 있는 가상 질문 3~5개"** 생성 → 각 질문 임베딩
- vector_store 에 질문 임베딩을 저장 (metadata 에 source section/문서 ID)
- query 와 매칭된 질문의 source 를 문서 단위로 dedup 후 반환
- LLM 가상 질문 생성은 256K 컨텍스트로 **문서당 1회 호출** (모든 섹션 질문을 한 번에 JSON 출력)

```
[인덱싱]
  문서 본문 (여러 섹션)
      ↓ LLM 1회 호출
  {
    "section_1": ["AuthService는 어떻게 토큰을 검증하나?",
                  "토큰 만료 시 동작은?",
                  "AuthService의 의존성은?"],
    "section_2": ["TokenStore의 인덱스 구조는?", ...]
  }
      ↓ 각 질문을 임베딩
  vector_store 에 질문 임베딩 + metadata(doc_id, section_path, question_text)
```

| 측면 | 영향 |
|------|------|
| 인덱싱 비용 | 옵션 D 대비 +1 LLM 호출/문서, 임베딩 호출 +50~100% |
| 검색 정밀도 | **대폭 ↑** (특히 정의/방법 질의에서) |
| LLM 추가 호출 | 문서당 1회 (R2 의 본문 그래프 호출과 합쳐 동일 흐름) |
| 출처 라벨 | "매칭 질문: ~~ / 섹션: ~~" 같은 풍부한 시그널 |
| 구현 난이도 | ★★ (LLM 프롬프트 + 신규 모듈) |

### D-3: D-1 + D-2 결합

- 문서 전체 + 섹션 본문 + 섹션 가상 질문 모두 등록
- 가장 정밀하지만 비용도 가장 큼

| 측면 | 영향 |
|------|------|
| 인덱싱 비용 | 옵션 D 대비 +1 LLM 호출, 임베딩 호출 ×2~3 |
| 검색 정밀도 | 최고 |
| 저장 공간 | 옵션 D 대비 ×2~3 |

## 어떤 변형이 사용자 의도에 가장 맞나

| 사용자 의도 | D-1 | D-2 | D-3 |
|------------|-----|-----|-----|
| "청크사이즈로 나누는 것을 없애" — 임의 토큰 분할 제거 | ✅ 섹션 단위 | ✅ 섹션 단위 | ✅ |
| "문서단위로 인덱싱" — 결과는 문서 | ✅ dedup | ✅ dedup | ✅ |
| "검색 정밀도 높이기" (이번 아이디어) | △ (부분 매칭만) | ✅ 질문 매칭 | ✅ 최대 |
| "256K LLM 활용" | (LLM 미사용) | ✅ 문서당 1회 | ✅ |
| 인덱싱 비용 보수적 | ✅ | △ | ❌ |

**권고**: **D-2** (가상 질문 임베딩) — 사용자 아이디어 직역이고, 256K LLM 활용 의도와도 정합. D-1 은 가성비 좋은 baseline 으로 그 위에 D-2 를 얹는 단계적 도입도 가능 (= D-3).

## D-2 구현 명세 (초안)

### 신규 모듈 `processor/question_generator.py`

```python
async def generate_questions_for_document(
    *,
    doc_title: str,
    extracted: ExtractedDocument,
    llm_client: LLMClient,
    config: QuestionGenConfig | None = None,
) -> dict[str, list[str]]:
    """문서 본문을 1회 LLM 호출로 처리해 섹션별 가상 질문 리스트를 반환.

    Returns:
        {section_id: [question1, question2, ...]} 매핑.
        섹션이 없으면 {"__doc__": [질문들]}.
    """
```

프롬프트 예시:
```
당신은 사내 위키 문서를 색인하는 검색 엔지니어입니다.
아래 문서의 각 섹션이 "답할 수 있는" 자연스러운 사용자 질문을 3~5개씩 생성하세요.

규칙:
- 본문에 답이 명시적으로 있는 질문만
- "X는 무엇인가", "Y는 어떻게 동작하나" 같은 자연 질의 형태
- 같은 의미의 질문 중복 금지

출력 (JSON):
{
  "sections": [
    {"section_id": "...", "section_path": "A > B", "questions": [...]},
    ...
  ]
}
```

### `pipeline.process_document` 변경

청킹/임베딩 단계가 다음으로 바뀐다:

```
extracted = extract_confluence(raw_html)

# 1. 본문 임베딩
section_chunks = chunk_by_section(extracted, max_tokens=8000)
# 작은 문서면 1개, 큰 문서면 여러 개

# 2. 가상 질문 생성 (256K LLM 1회 호출)
questions_by_section = await generate_questions_for_document(
    doc_title=title,
    extracted=extracted,
    llm_client=llm_client,
)

# 3. 임베딩 등록 (벡터 종류 3가지)
embeddings_to_add = []
for chunk in section_chunks:
    # 본문 임베딩
    embeddings_to_add.append({
        "id": f"{chunk.id}#body",
        "text": chunk.content,
        "view": "body",
    })
    # 메타 임베딩 (title + section_path)
    embeddings_to_add.append({
        "id": f"{chunk.id}#meta",
        "text": f"{title}\n{chunk.section_path}",
        "view": "meta",
    })
    # 가상 질문 임베딩
    for i, q in enumerate(questions_by_section.get(chunk.section_id, [])):
        embeddings_to_add.append({
            "id": f"{chunk.id}#q{i}",
            "text": q,
            "view": "question",
        })

# 4. 임베딩 호출 (배치) + vector_store 등록
```

### `_search_chunks` dedup 확장

```python
# 현재: logical_chunk_id 로 dedup (body/meta 두 뷰가 같은 청크면 1개로)
# 변경: document_id 단위 dedup 추가 옵션
key = meta.get("document_id") if dedup_by_document else meta.get("logical_chunk_id")
```

다만 한 문서에서 여러 섹션이 매칭되면 정말 dedup 할지 결정 필요:
- 옵션 (a): 문서 단위 dedup — 최상위 매칭 1건만 반환 (사용자 의도 직역)
- 옵션 (b): 문서 단위 그루핑 — 같은 문서의 매칭들을 묶어 반환 (출처 라벨 풍부)

권장: **(b) 그루핑**. dedup 으로 정보 손실 없이 "이 문서의 어느 섹션/질문이 매칭됐는지" 모두 보여줌.

### 새 metadata 스키마 (vector_store)

기존:
```python
{document_id, chunk_index, title, section_path, section_anchor,
 logical_chunk_id, view: "body"|"meta"}
```

신규:
```python
{document_id, chunk_index, title, section_path, section_anchor,
 logical_chunk_id, view: "body"|"meta"|"question",
 question_text: "..."}  # view="question" 일 때만
```

### MCP 답변 컨텍스트 조립

기존: `max_context_chunks=10` 청크를 4096 토큰 안에 결합.

신규:
- 매칭된 벡터를 `document_id` 로 그루핑
- 문서별로: 매칭된 섹션 본문 + (선택) 매칭된 가상 질문 표시
- `max_context_docs=5` 같은 새 파라미터 (현재 chunks 단위와 호환)
- `context_max_tokens` 는 32K 정도로 상향 (256K LLM 활용)

## 트레이드오프 매트릭스 — D-2 vs 옵션 D (단순 1벡터)

| 항목 | D (1벡터 + 섹션 폴백) | D-2 (1벡터 + 섹션 + 가상 질문) | Δ |
|------|---------------------|---------------------------|---|
| 인덱싱 LLM 호출/문서 | R2 본문 그래프 1회 | R2 본문 그래프 1회 + 가상 질문 1회 = **2회** | +1 |
| 인덱싱 임베딩 호출/문서 | 평균 2~6개 (body + meta × 섹션 수) | 평균 8~24개 (+가상 질문 5~15개) | ×3~4 |
| 검색 정밀도 (정의/방법 질의) | 보통 | **대폭 ↑** | +++ |
| 검색 정밀도 (키워드 질의) | 보통 | ↑ (본문 임베딩이 여전히 작동) | + |
| 결과 입자도 | 문서 | 문서 (dedup) | = |
| 출처 라벨 | section_path | section_path + 매칭 질문 | + |
| 구현 LOC | ~150 | ~400 (신규 모듈 + dedup 확장 + 프롬프트) | ×2.5 |
| 회귀 위험 | 낮음 | 중간 (vector_store metadata 확장, search dedup 변경) | + |

## D-2 구현 단계 제안 (점진적)

핵심 위험: 가상 질문 LLM 호출의 품질 검증 없이 전체 인덱싱을 한 번에 전환하면 정밀도 회귀 시 롤백 비용이 큼.

**1단계** (이번 PR): D-1 도입 — 섹션 단위 청킹 + 문서 단위 dedup 옵션 추가
  - 가상 질문 없음
  - vector_store 스키마 무변경
  - 검색 정밀도 측정 가능한 baseline 확보

**2단계** (다음 PR): D-2 점진 도입 — 가상 질문 생성기 추가
  - `enable_question_indexing: bool = False` 기본 OFF
  - 운영자가 ON 으로 전환 → 일부 문서로 측정
  - 기존 벡터와 공존 (vector_store metadata view="question" 추가)

**3단계** (운영): 측정 후 기본 ON 또는 폐기 결정

또는 한 번에 D-2 도입 — 사용자 선호에 따라.

## 결정 필요

1. **D-1 / D-2 / D-3 중 어느 변형?**
2. **단계적 도입(D-1 → D-2) vs 한 번에 D-2?**
3. 가상 질문 생성 LLM 프롬프트 톤 — 자연 질의(사용자 표현) vs 검색 키워드(나열형) 둘 다 / 골라서
