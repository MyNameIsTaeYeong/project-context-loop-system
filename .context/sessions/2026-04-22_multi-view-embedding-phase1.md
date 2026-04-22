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

---

## 후속: 대시보드 청크 탭에 meta 뷰 노출 (같은 날, 옵션 3)

운영자가 "이 청크가 무엇으로 임베딩됐는지"를 브라우저에서 직접 확인할 수 있도록 대시보드에 노출.

### 변경

**Phase A — 데이터 레이어**
- `chunks` 테이블에 `section_path TEXT DEFAULT ''` + `section_anchor TEXT DEFAULT ''` 컬럼 추가.
- `_migrate_schema()` 에 idempotent ALTER. 구버전 DB는 `PRAGMA table_info(chunks)` 검사 후 컬럼이 없을 때만 추가, 기존 row는 빈 문자열로 채워짐 (D3-A 정책).
- `create_chunk()` 시그니처에 두 파라미터(기본값 `""`) 추가.
- `processor/pipeline.py` 두 분기(git_code 분기 L155, 일반 분기 L253) 모두 `chunk.section_path`/`chunk.section_anchor` 를 전달.

**Phase B — API**
- `pipeline._build_meta_view_text` → `pipeline.build_meta_view_text(title, section_path)` 로 시그니처 변경 + public.
- `web/api/documents.py::tab_chunks` 가 각 청크에 `meta_text` 필드를 합성해 템플릿에 전달. 매 요청마다 결정론적 재구성(별도 저장 없음, D2-A).

**Phase C — 템플릿**
- `tab_chunks.html` 재작성: 청크별로 `<details open>` Body + `<details>` Meta 두 섹션. 헤더에 `section_path` 와 `body + meta` / `body only` 뱃지 표시.
- 기존 Pico CSS 컨벤션(article/header/details/summary) 유지.

### 테스트

- `test_metadata_store.py` +2:
  - `test_migration_adds_section_columns_to_legacy_chunks`: 구버전 DB에서 컬럼 추가 + 기존 row는 빈 문자열로 채워짐.
  - `test_chunks_crud` 확장: section_path/anchor 왕복 + 기본값 `""` 검증.
- `test_pipeline.py` +1:
  - `test_build_meta_view_text_combinations`: title-only / path-only / 둘 다 / 둘 다 없음 / 공백 트리밍.
  - 기존 `test_multi_view_embeddings_stored_for_chunks` 에 SQLite 청크의 section_path/anchor 보존 검증 추가.
- 비-web 전체: 452건 통과.
- **수동 UI 자동화 한계**: 선재 Jinja2 캐시 이슈로 `test_web/` 가 14건 깨져있어 청크 탭 템플릿 렌더 검증을 자동화하지 못함. 대신 임시 DB + `build_meta_view_text` 직접 호출 스모크 테스트로 데이터 경로(섹션 정보 → enrichment → meta_text)를 검증.

### 결정 정합성 (D-042 후속)

- D1-A: SQLite 컬럼 추가 채택 (조회 단순, 향후 deep-link/검색 결과에도 재사용)
- D2-A: meta 텍스트 재구성 (저장 안 함, 규칙 변경 시 마이그레이션 불요)
- D3-A: 구버전 청크는 그대로 두고 신규 처리만 채움 (재처리는 사용자 트리거)

---

## 후속 보강: git_code 청크 표시 정정 (같은 날)

### 발견된 버그

대시보드 청크 탭이 모든 source_type에 같은 템플릿을 적용해, git_code 문서의 청크에도:
- `chunk.content` 를 "Body (임베딩 대상)"로 표시 → **거짓** (실제 임베딩 입력은 `embed_texts`)
- `build_meta_view_text(...)` 결과를 "Meta (추가 임베딩 대상)"로 표시 → **거짓** (git_code는 ChromaDB에 `#meta` 엔트리 자체가 없음)

원인: D-036의 임베딩/저장 분리 패턴(git_code)과 D-042 멀티뷰(일반 분기)는 다른 구조인데, 청크 탭이 이를 구분하지 않았음.

### 수정

**스키마**: `chunks.embed_text TEXT DEFAULT ''` 컬럼 추가 + idempotent ALTER 마이그레이션.

**파이프라인**: git_code 분기에서 `create_chunk(embed_text=embed_texts[i])` 로 임베딩 입력 영속화. 일반 분기는 빈 문자열(본문 자체가 임베딩 입력이라 저장 불요).

**API**: `tab_chunks` 가 `doc.source_type` 조회 후 git_code면 meta_text 합성을 생략하고 source_type을 템플릿에 전달.

**템플릿**: source_type 분기.
- git_code: "Stored Content (반환용 — 임베딩 대상 아님)" + "Embedding Text (이름+시그니처+docstring — 실제 입력)". 뱃지 `code · single vector`.
- 그 외: 기존 "Body" + "Meta" 유지.

기존 git_code 청크는 embed_text가 비어있으면 "재처리 시 채워집니다" 안내 표시.

### 테스트

- `test_metadata_store.py`:
  - 마이그레이션 테스트가 `embed_text` 컬럼도 검증.
  - `test_chunks_crud` 에 embed_text 왕복 + 기본값 검증.
- `test_pipeline.py`:
  - 신규 `test_git_code_pipeline_persists_embed_text`: 실제 Python 코드를 입력으로 process_document 실행, ChromaDB add_chunks가 #body/#meta 접미사를 안 붙이는지 검증, SQLite에 embed_text가 채워지고 본문과 다른 값(D-036 분리)인지 검증.
- 비-web 전체: 453건 통과 (+1).

### 데이터 경로 스모크 검증

git_code 문서와 Confluence 문서를 한 DB에 만들고 청크 탭 enrichment 로직을 직접 호출:

```
=== git_code ===
  [Stored Content] '# File: src/x.py\n# function: def hello\n...'
  [Embedding Text] 'src/x.py\nhello\ndef hello()'

=== confluence ===
  [Body] '| prod | 이운영 |'
  [Meta] '배포 가이드\n배포 가이드 > 운영'
```

두 source_type이 의도대로 다른 표시 경로를 탐.
