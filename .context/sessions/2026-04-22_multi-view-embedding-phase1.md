# 멀티뷰 임베딩 Phase 1 — body + meta 두 벡터 도입

- 일시: 2026-04-22
- 브랜치: `claude/clarify-step3-examples-YxNCq`
- 관련 결정: D-042

## 배경

D-041 이후 청크의 `section_path`·`title`이 metadata에만 저장되고 임베딩 텍스트엔 반영되지 않았다. "운영 챕터", "배포 가이드" 같이 **섹션 명칭으로 묻는 질의**가 본문 키워드 부재 청크(표/코드 전용 청크 등)에 제대로 매칭되지 않는 한계가 있었다.

D-036의 "임베딩 텍스트 ≠ 저장 텍스트" 분리 원칙을 Confluence에도 적용하되, 기존 본문 임베딩을 **대체**하는 대신 **나란히** 추가하는 설계(멀티뷰)를 택했다. 합성 단일벡터(path+body 결합)는 두 신호를 평균해 약화시키는 반면, 멀티벡터는 뷰별 독립 경쟁으로 max 연산처럼 동작한다.

## 변경 내용

### 1. 파이프라인 — 뷰별 엔트리 생성 (`processor/pipeline.py`)

- 일반 문서 분기(Confluence/upload/manual, git_code 제외)에서 청크당 2개 ChromaDB 엔트리 저장:
  - `{chunk.id}#body`: 임베딩=본문, metadata.view="body"
  - `{chunk.id}#meta`: 임베딩=`title + section_path`, metadata.view="meta"
- 두 엔트리의 `document`(반환 본문)는 동일.
- `logical_chunk_id`를 metadata에 공유.
- `aembed_documents` 배치 하나로 `body_texts + active_meta_texts`를 이어붙여 1회 호출.
- `title`/`section_path`가 모두 비면 meta 뷰 생략.
- SQLite `chunks` 테이블은 여전히 논리 청크당 1행.
- 헬퍼 `_build_meta_view_text(title, chunk)` 추가.

### 2. 검색 — over-fetch + dedup (`mcp/context_assembler.py::_search_chunks`)

- `n_results = max_chunks * 2`로 과잉 인출.
- `logical_chunk_id` (없으면 `id` 폴백) 기준 dedup.
- ChromaDB가 거리 오름차순으로 반환하므로 먼저 등장한(최소 distance) 엔트리를 채택 — 자연스럽게 max-similarity 선택.
- `similarity_threshold` 필터는 dedup 후 적용.

### 3. 테스트

- `tests/test_processor/test_pipeline.py` +2
  - `test_multi_view_embeddings_stored_for_chunks`: body + meta 두 ID, document 동일, metadata.view/logical_chunk_id 검증, SQLite는 1행.
  - `test_meta_view_skipped_when_title_and_path_empty`: title/section_path 둘 다 빈 경우 body만 저장.
  - 기존 mock embedding이 고정 1건만 반환하던 걸 입력 길이만큼 반환하도록 교체.
- `tests/test_mcp/test_context_assembler.py` +1
  - `test_search_chunks_dedupes_multi_view_entries`: 동일 `logical_chunk_id` 엔트리 dedup, 최소 distance 항목 유지.

## 테스트 결과

```
tests/ (test_web 제외): 450 passed
```

기존 web 계층 실패 14건은 Jinja2 환경 문제로 스코프 밖(스타시 후 확인).

## 한계 / 후속 작업 후보

- Phase 2: 섹션 본문 앞 1~2문장을 "도입부 뷰"로 추가 (선택).
- Phase 3: LLM 생성 요약 뷰 (비쌈, A/B 측정 후).
- 기존 문서는 재처리 전까지 body 뷰만 존재 — 일괄 재처리 기능이 있다면 운영 단계에서 1회 트리거하면 전체 인덱스가 멀티뷰로 업그레이드됨.
- meta 뷰 유사도 과잉 획득이 측정되면 `view == "meta"`에 가중 페널티(예: 0.85) 후처리 옵션 검토.
