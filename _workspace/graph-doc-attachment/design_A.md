# 설계안 A — 그래프 엔티티 문서의 컨텍스트 직접 첨부

> 목표: 그래프 검색이 도달한 엔티티들의 **문서 본문(관련 청크)**을 컨텍스트에 첨부한다.
> 현재는 엔티티/관계 텍스트만 넣고, 그 문서의 내용은 LLM에 전달되지 않는다.
> 기준 코드: 현재 HEAD. 분석/설계 — 코드 변경 없음.

## 1. 현재 상태 (확인된 사실)

`src/context_loop/mcp/context_assembler.py`에 두 진입 함수가 있고, 둘 다 그래프 문서 본문을 컨텍스트에 넣지 않는다.

| 함수 | 그래프 처리 | 그래프 문서 본문 |
|------|------------|-----------------|
| `assemble_context` (라인 57, 텍스트 반환) | `sections.append(graph_result.text)` (라인 134) | **미포함** |
| `assemble_context_with_sources` (라인 341, AssembledContext 반환) | `graph_result.text` + `document_ids`를 `Source(similarity=0.0)`로 출처에만 추가 (라인 440-450) | **미포함** (출처 라벨만, 내용 없음) |

핵심: `GraphSearchResult.document_ids`는 이미 수집되어 있다 (`graph_search_planner.py:185, 518-528`). `with_sources`는 이걸 **출처 목록**에는 넣지만 **본문은 인출하지 않는다.** → 이 갭을 메우는 게 설계 A.

재사용 가능한 자산:
- `vector_store.search(query_embedding, n_results, where=...)` — `where` 필터를 ChromaDB에 그대로 전달 (`vector_store.py:100`). → `where={"document_id": {"$in": [...]}}`로 **특정 문서들만 대상**으로 쿼리 유사도 검색 가능.
- `_search_chunks`의 **document_id 단위 dedup** 패턴 (`context_assembler.py:199-212`).
- `query_embedding`은 이미 계산되어 함수 내에 존재.
- `_extract_doc_ids(chunk_results)` (라인 483) — 벡터가 이미 찾은 문서 집합.

## 2. 설계 개요

그래프가 `document_ids`를 내놓으면:
1. **벡터가 이미 찾은 문서 제외** (`graph_doc_ids - vector_doc_ids`) → 그래프의 *순수 추가분*만 대상.
2. 남은 문서들에 대해 **쿼리 임베딩으로 가장 관련된 청크**를 인출 (`where $in` 필터).
3. 문서당 1청크(최소 distance) dedup + **상한 `max_graph_docs`**.

> **중요 — 청크는 문서 단위다.** confluence는 `chunk_extracted_document_doclevel`로
> **작은 문서 = 1청크 = 문서 전체**, 큰 문서(>`max_embedding_tokens`=8000)만 섹션
> 폴백한다 (`chunker.py:387`). 따라서:
> - 작은 문서(대부분): "관련 청크 1개" = **문서 통째**. 슬라이싱 효과 없음.
> - 큰 문서: 섹션 청크 중 쿼리에 가장 가까운 1개 = 진짜 슬라이스.
> - 쿼리 랭킹의 실제 역할: ① **여러 그래프 문서 중 선별**(문서당 1청크 dedup이
>   doc-level 인덱싱과 자연 정합) ② 큰 문서의 섹션 선택.
> - **토큰 예산이 핵심 제약**이 된다(§7-2 참조) — doc-level이라 그래프 문서 1개가
>   최대 8000토큰까지 차지할 수 있다.
4. 별도 섹션 `## 그래프 연결 문서`로 포맷팅 → LLM·평가가 벡터 경로와 구분 가능.
5. 기존 엔티티/관계 텍스트(`graph_result.text`)는 "왜 이 문서인가" 경량 요약으로 유지.

이 설계는 앞서 논의한 "겹치면 추가 가치 없음" 우려를 구조적으로 해소한다 — 겹치는 문서는 1번에서 빠지므로 **net-new 문서만** 첨부된다.

## 3. 신규 헬퍼

`context_assembler.py`에 추가:

```python
async def _search_graph_sourced_chunks(
    query_embedding: list[float] | None,
    vector_store: VectorStore,
    graph_doc_ids: set[int],
    existing_doc_ids: set[int],
    *,
    max_graph_docs: int,
) -> list[dict[str, Any]]:
    """그래프가 도달한 문서 중 벡터가 못 찾은 것들의 가장 관련된 청크를 인출.

    그래프는 관계로 문서에 도달하지만, 첨부는 쿼리에 가장 가까운 청크 1개로
    제한하여 컨텍스트 예산을 보호한다. 벡터 결과와 겹치는 문서는 제외한다.
    """
    if query_embedding is None or max_graph_docs <= 0:
        return []
    new_doc_ids = [d for d in graph_doc_ids if d not in existing_doc_ids]
    if not new_doc_ids:
        return []
    try:
        # 멀티뷰(body/meta/question) 고려 over-fetch + 문서 단위 dedup
        raw = vector_store.search(
            query_embedding,
            n_results=len(new_doc_ids) * 6,
            where={"document_id": {"$in": new_doc_ids}},
        )
    except Exception:
        logger.warning("그래프 문서 청크 검색 실패", exc_info=True)
        return []

    seen: set[Any] = set()
    deduped: list[dict[str, Any]] = []
    for r in raw:  # distance 오름차순 도착 → 문서당 첫 항목이 최소 distance
        meta = r.get("metadata") or {}
        key = meta.get("document_id") or meta.get("logical_chunk_id") or r.get("id")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
        if len(deduped) >= max_graph_docs:
            break
    return deduped
```

포맷터 (벡터용 `_format_chunk_results`와 구분):

```python
async def _format_graph_chunk_results(
    results: list[dict[str, Any]],
    meta_store: MetadataStore,
) -> str:
    lines = ["## 그래프 연결 문서"]   # 벡터 "## 관련 문서" 와 구분
    doc_cache: dict[int, str] = {}
    for r in results:
        meta = r.get("metadata") or {}
        doc_id = meta.get("document_id")
        if doc_id and doc_id not in doc_cache:
            doc = await meta_store.get_document(doc_id)
            doc_cache[doc_id] = doc["title"] if doc else f"문서 #{doc_id}"
        title = doc_cache.get(doc_id, "알 수 없음") if doc_id else "알 수 없음"
        section_path = meta.get("section_path", "")
        header = f"\n### [{title}] (그래프 경로로 도달)"
        if section_path:
            header += f"\n_섹션: {section_path}_"
        lines.append(header)
        lines.append(r.get("document", ""))
    return "\n".join(lines)
```

## 4. 통합 (두 함수 공통 패턴)

`assemble_context` — 라인 133-134 교체:

```python
if graph_result:
    sections.append(graph_result.text)                      # 기존: 구조 요약
    existing = _extract_doc_ids(chunk_results)
    graph_chunks = await _search_graph_sourced_chunks(
        query_embedding, vector_store,
        graph_result.document_ids, existing,
        max_graph_docs=max_graph_docs,
    )
    if graph_chunks:
        sections.append(
            await _format_graph_chunk_results(graph_chunks, meta_store)
        )
```

`assemble_context_with_sources` — 라인 440-450에 동일 인출을 추가하고, **출처 similarity를 실제 값으로 보강**:

```python
if graph_result:
    sections.append(graph_result.text)
    existing = _extract_doc_ids(chunk_results)
    graph_chunks = await _search_graph_sourced_chunks(
        query_embedding, vector_store,
        graph_result.document_ids, existing,
        max_graph_docs=max_graph_docs,
    )
    fetched_sim: dict[int, float] = {}
    if graph_chunks:
        sections.append(await _format_graph_chunk_results(graph_chunks, meta_store))
        for r in graph_chunks:
            did = (r.get("metadata") or {}).get("document_id")
            if did is not None:
                fetched_sim[did] = 1 - r.get("distance", 1.0)
    # 기존 출처 추가 로직 유지하되 similarity 보강
    existing_doc_ids = {s.document_id for s in sources}
    for doc_id in graph_result.document_ids:
        if doc_id not in existing_doc_ids:
            ... title 조회 ...
            sources.append(Source(
                document_id=doc_id, title=title,
                similarity=fetched_sim.get(doc_id, 0.0),  # 본문 인출된 건 실제 유사도
            ))
```

## 5. 파라미터 / 설정 plumbing

| 위치 | 변경 |
|------|------|
| `assemble_context` / `assemble_context_with_sources` | `max_graph_docs: int = 3` 인자 추가 |
| `mcp/tools.py::search_context` (라인 20) | `max_graph_docs: int = 3` 인자 추가 → `assemble_context`에 전달 |
| `config/default.yaml` `mcp:` 섹션 (라인 212) | `max_graph_context_docs: 3` 추가 (기존 `max_context_chunks: 10`, `context_max_tokens: 32768` 옆) |
| server/tool 초기화 | config 값을 `search_context` 기본으로 주입 |

기본값 `3`은 보수적. 단, 청크가 doc-level이라 개수만으로는 부족하므로 **토큰 상한 병행**
(§7-2): 그래프 첨부 합계 ~6000토큰 가드. `max_graph_docs=0`이면 기능 off(= 현재 동작)
→ 안전한 롤백 스위치이자 "관계 자체가 답인 질의" 대응.

추가 설정 후보: `config/default.yaml mcp.max_graph_context_tokens: 6000`.

## 6. 엣지 케이스 (동작 정의)

| 케이스 | 동작 |
|--------|------|
| 그래프 문서가 벡터 결과와 전부 겹침 | `new_doc_ids` 비어 → 그래프 청크 0개 (요약만). 추가 가치 없을 때 노이즈 0. |
| 그래프 노드가 ChromaDB에 벡터 없음 (예: 미임포트 페이지 참조, link_graph가 만든 외부 document 노드) | `where $in` 결과 없음 → 본문 없이 출처 라벨만 (기존 동작 유지). |
| `query_embedding is None` (임베딩 실패) | 그래프 청크 0개 (안전). |
| `max_graph_docs=0` | 기능 off. |
| 그래프 문서 다수(멀티홉) | 쿼리 유사도 랭킹 + 상한으로 가장 관련된 N개만. |

## 7. 리스크 / 트레이드오프

1. **추가 벡터 쿼리 1회** (`where $in` search). LLM 호출 아님 → 지연 영향 작음. 그래프 검색이 이미 일어난 경우에만 실행.
2. **토큰 예산 (doc-level 청크의 핵심 제약)** — 청크가 문서 단위라 그래프 문서 1개가
   최대 `max_embedding_tokens`(8000)까지 차지한다. 벡터 `max_chunks=10`도 동일하게
   doc-level이므로, 단순 개수 상한(`max_graph_docs`)만으로는 `context_max_tokens=32768`을
   초과할 위험이 있다. → **개수 상한 + 토큰 상한 병행** 권장:
   - `_search_graph_sourced_chunks`에서 dedup 청크를 누적할 때 `chunk.token_count`(또는
     `count_tokens(document)`)를 합산하여 잔여 토큰 예산(`context_max_tokens` − 벡터 청크
     토큰 합)을 넘으면 중단.
   - 기본값은 보수적으로: `max_graph_docs=3` + 그래프 첨부 토큰 상한 ~6000(전체 예산의 ~20%).
3. **`$in` 리스트 크기** — 멀티홉으로 doc_id가 매우 많으면 필터가 커진다. 입력 `new_doc_ids`를 상한(예: 50)으로 자른 뒤 검색하면 안전.
4. **리랭커와의 상호작용** — 1차 설계는 그래프 청크를 쿼리 임베딩 유사도로만 랭킹(리랭커 미적용)하여 변경 범위 최소화. 필요 시 후속으로 그래프 청크도 `rerank()`에 통과시키는 옵션 추가.
5. **관계가 답인 질의** — 문서 첨부가 약한 노이즈가 될 수 있음 → `max_graph_docs` 낮게(3) + 별도 섹션으로 분리하여 LLM이 구분 가능.

## 8. 테스트 계획

**단위 (`tests/test_mcp/`):**
- `_search_graph_sourced_chunks`: ① 벡터 기존 문서 제외 ② 문서 단위 dedup(문서당 1청크) ③ `max_graph_docs` 상한 ④ 벡터 없는 doc_id 무시 ⑤ `query_embedding None`/`max_graph_docs=0` → 빈 리스트.
- `_format_graph_chunk_results`: 헤더 `## 그래프 연결 문서` + 제목/섹션 라벨.

**통합:**
- 시나리오: 문서 X가 벡터 검색에는 안 잡히지만 그래프 관계로 도달 → 컨텍스트에 X의 관련 청크가 `## 그래프 연결 문서` 아래 등장.
- 시나리오: 그래프 문서가 벡터 결과와 전부 겹침 → 그래프 청크 섹션 없음(요약만).
- `with_sources`: 본문 인출된 그래프 문서의 `Source.similarity`가 0.0이 아닌 실제 값.

**평가 (선택, ROI 확인):**
- 그래프 doc_ids ∩ 벡터 doc_ids 겹침률 로깅 → 겹침 낮으면 설계 A의 순수 추가 가치 큼.
- eval gold set로 answer recall/정확도 델타 측정 (`max_graph_docs` 0 vs 3 vs 5).

## 9. 변경 파일 요약

| 파일 | 변경 |
|------|------|
| `src/context_loop/mcp/context_assembler.py` | `_search_graph_sourced_chunks`, `_format_graph_chunk_results` 추가; 두 assemble 함수에 통합; `max_graph_docs` 인자 |
| `src/context_loop/mcp/tools.py` | `search_context`에 `max_graph_docs` 추가 + 전달 |
| `config/default.yaml` | `mcp.max_graph_context_docs: 3` |
| (server/tool 초기화 코드) | config → 기본값 주입 |
| `tests/test_mcp/test_context_assembler.py` | 단위/통합 테스트 |

## 10. 구현 순서 (제안)

1. `_search_graph_sourced_chunks` + `_format_graph_chunk_results` + 단위 테스트.
2. `assemble_context`에 통합 + 통합 테스트.
3. `assemble_context_with_sources`에 통합(출처 similarity 보강) + 테스트.
4. `tools.py` + `config` plumbing.
5. (선택) 겹침률 로깅 + eval 델타 측정.

각 단계가 독립적으로 동작/롤백 가능(`max_graph_docs=0`).
