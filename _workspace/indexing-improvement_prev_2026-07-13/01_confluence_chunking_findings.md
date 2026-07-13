# Confluence Chunking — Round 2 Findings: 청킹 제거 타당성

> 본 보고서는 **`source_type='confluence_mcp'` 청킹 파이프라인을 제거하고 문서 단위 인덱싱으로 전환할 수 있는가** 에 초점을 맞춘다. R1(`_workspace/indexing-improvement_prev_R1/01_confluence_chunking_findings.md`) 에서 다룬 청킹 내부 버그(F-01 ~ F-12)는 모두 반영 완료된 것으로 가정하고 중복하지 않는다.

## 요약

- 핵심 결론: **하이브리드 조건부 가능** — 청킹 코드를 통째로 제거하는 것은 다음 4가지 다운스트림 결합 때문에 권장하지 않음. 다만 청킹 입자도를 ExtractionUnit 수준(~1500~2400 토큰)으로 통합하고, 작은 문서는 1청크로 처리하는 단순화는 분명한 이득이 있음.
- 핵심 제약: **임베딩 모델(`nomic-embed-text` 8192 토큰)이 단일 하드 시그널**. 임베딩 호출 자체는 잘리지 않더라도 단일 벡터로 압축되는 길이가 5K 토큰을 넘으면 정밀도 손실이 실측 사례에서 누적됨.
- 영향이 큰 다운스트림(원인 → 영향):
  1. **벡터 검색 정밀도** — body+meta 멀티뷰 dedup(`logical_chunk_id`)와 청크별 `section_path` 헤더가 문서당 1벡터로 합쳐지면 `assemble_context_with_sources`(`context_assembler.py:413`) 의 "섹션: …" 출처 라벨이 빈 문자열이 됨.
  2. **LLM 본문 그래프 추출** — `extract_llm_body_graph` (`llm_body_extractor.py:319`) 의 user prompt 가 `unit.content`(breadcrumb + body) 전체이며, max_tokens=32768 응답 예산을 추가로 차감한 후 qwen2.5:7b 의 32K 컨텍스트로 들어가야 함. 문서 전체가 1 unit 이 되면 응답 잘림(JSON 파싱 실패) 확률 급증.
  3. **결정론 본문 그래프(`body_extractor`)의 section_label** — Relation.label 에 첫 등장 unit 의 `section_path` 를 기록(`body_extractor.py:23` 모듈 docstring 명시). 문서 단위 1 unit 이면 모든 관계가 동일 label 을 가지게 되어 그래프 카드의 "어디서 나온 관계인가" 시그널 손실.
  4. **재랭킹/MCP 응답 토큰 예산** — `context_assembler.assemble_context_with_sources` 가 청크 N건을 그대로 LLM 컨텍스트에 직렬 결합. 문서 1건당 평균 5K~50K 토큰이 통째로 박히면 `mcp.context_max_tokens=4096` 가드(설정 기본)와 즉시 충돌.

## 1. 현재 청킹의 강제 요인 (코드 근거)

### 1.1 `chunker.chunk_extracted_document` 의 분할이 강제되는 진짜 이유

- **위치**: `src/context_loop/processor/chunker.py:329-384`
- **호출처**: `pipeline.process_document` → `chunks = chunk_extracted_document(extracted, chunk_size=cfg.chunk_size=512, …)` (`pipeline.py:231-243`)
- **분할 산출물의 4가지 소비처** (한 곳도 빠지면 안 됨):
  1. `vector_store.add_chunks(vec_ids, embeddings, documents, metadatas)` — ChromaDB 에 청크-단위 벡터 저장 (`pipeline.py:285`).
  2. `meta_store.create_chunk(...)` — SQLite `chunks` 테이블에 청크-단위 row (`pipeline.py:289-300`). 이게 대시보드 "청크 탭"(`tab_chunks.html`)의 데이터 소스.
  3. `_search_chunks` 결과의 `metadata.section_path` — `context_assembler.py:236, 413` 의 출처 라벨 + `section_anchor` 의 Confluence deep-link.
  4. `logical_chunk_id` dedup — 멀티뷰(body/meta) 임베딩이 같은 본문에 대해 두 엔트리로 저장되므로, dedup 키가 청크 단위에서 작동해야 함(`context_assembler.py:201`).

- **분할의 진짜 강제 요인**: **`chunk_size=512` 는 임베딩 정밀도 가드이지 모델 한도 제약이 아니다.** 코드 어디에도 `nomic-embed-text` 의 8192 토큰 한도를 의식한 가드(`if token_count > 8192: split`)가 없고, 512 는 단순히 "한 벡터가 표현할 수 있는 의미 단위" 의 휴리스틱이다. 이는 다음 두 가지에서 확인됨:
  - atomic 블록(코드/표) 은 "chunk_size 를 초과해도 자르지 않고 단독 청크로 방출"(`chunker.py:445-453`). 즉, **이미 oversized 청크가 정책상 허용됨** → "임베딩 모델 한도가 진짜 제약이라면 atomic 블록도 임베딩 가능해야 하는데 8192 초과 시 처리가 없음**" 이 정합이 안 맞는 부분이며, 거대 표가 들어간 페이지는 사실상 정밀도 손실로 굴러가고 있을 가능성.
  - `chunker` 는 토큰 카운트만 보고 분할하며 **임베딩 클라이언트의 모델 한도를 인지하지 않는다.** `EndpointEmbeddingClient`(`embedder.py:25`) 가 `_BATCH_SIZE=100` 만 가드.

### 1.2 `extraction_unit.split_into_units` 의 분할이 강제되는 진짜 이유

- **위치**: `src/context_loop/processor/extraction_unit.py:147-201`
- **호출처**: `pipeline.process_document` 의 본문 그래프 추출 분기 (`pipeline.py:327-329`).
- **분할 정책**: `target_tokens=1500, max_tokens=2400, overlap_tokens=200` (`ExtractionUnitConfig` 기본값, `extraction_unit.py:56-59`).
- **분할의 진짜 강제 요인**: **LLM 본문 그래프 추출의 응답 품질 + 토큰 예산.**
  - `llm_body_extractor._call_llm` 이 unit 하나당 `max_tokens=32768` 응답 예산을 요청(`llm_body_extractor.py:77` 의 주석에 명시: "본문 1500 토큰 unit 에서 entities + relations JSON 응답이 1000+ 토큰으로 늘어날 수 있고, reasoning 모델은 thinking 토큰까지 예산을 잡아먹는다").
  - qwen2.5:7b 의 32K 컨텍스트 = (시스템 프롬프트 ~500토큰) + (user: breadcrumb + body) + (응답 예산 32768) 라는 등식에서 user 부분이 압축되어야 함. 1500 토큰 unit 은 안전 마진을 두고 설계됨.
  - **즉, `extraction_unit` 의 분할은 임베딩과 무관하며 순전히 LLM 호출의 응답 토큰 예산 확보용.**

### 1.3 `pipeline.process_document` 의 청크 결과 소비 흐름

- **위치**: `src/context_loop/processor/pipeline.py:209-371`
- **소비 채널 2종**:
  - **벡터 채널**(`pipeline.py:244-301`): chunks → body+meta 멀티뷰 임베딩 → ChromaDB + SQLite `chunks` 테이블.
  - **그래프 채널**(`pipeline.py:326-371`): extracted.sections → `build_extraction_units` → 1) 결정론 `extract_body_graph` 2) LLM `extract_llm_body_graph`.
- **관찰**: 두 채널은 **이미 다른 단위**(chunker = chunk_size=512, extraction_unit = target=1500)로 작동 중. 즉 청킹은 사실상 "두 종류의 분할 정책이 동시에 돌고 있는" 상태이며, 청크 제거가 그래프 추출에 미치는 영향과 임베딩에 미치는 영향이 분리 가능함.

## 2. 환경 한계 점검

### 2.1 LLM (`qwen2.5:7b` via Ollama)

- 공식 컨텍스트 윈도우: **32768 토큰**.
- 코드의 LLM max_tokens 요청값: **32768** (`llm_body_extractor.py:77`).
- 산수: 시스템 프롬프트(~500) + user 본문 X + 응답 예산 32768 ≤ 32768. → **X ≈ 0**. 응답 예산을 max_tokens 로 잡아두면 입력은 사실상 0 토큰이어야 함. 이건 Ollama `num_ctx` 의 동작 방식(입력+출력 합산이 num_ctx) 을 고려하면 **현재 설정은 이미 위험**.
- 실용 가정: 응답이 보통 ~1500 토큰 이내에 끝나므로 (entities + relations JSON) 실측 충돌은 적지만, **문서 전체를 user 에 박을 경우** Ollama 가 입력 잘림(`num_predict` 부족) 또는 thinking 모델일 때 잘림이 잦아짐.
- **결론**: 그래프 추출 LLM 의 안전 입력은 **현재 설계의 1500 토큰 unit 그대로 유지가 합리적**. 문서 전체로 확대하려면 max_tokens 를 함께 축소(예: 4096) 해야 하지만 그러면 응답 잘림 위험이 커짐. 트레이드오프 부재.

### 2.2 임베딩 (`nomic-embed-text`)

- 공식 컨텍스트 윈도우: **8192 토큰** (one-shot encode).
- 임베딩 차원: 768 (의미 정보가 압축되는 공간).
- 실증 휴리스틱(공개 RAG 평가들): **단일 임베딩 벡터의 의미 표현력은 토큰 수가 ~512~1500 구간에서 가장 안정**, 5K 토큰 이상에서는 평균화 효과로 정밀도 저하 (특히 다중 토픽 문서). 이는 모델 한도와 별개의 정밀도 가드.
- **결론**: `nomic-embed-text` 가 8K 까지 안 잘리고 받지만, "1 문서 = 1 벡터" 가 의미적으로 합당한 길이는 보통 ~2K 토큰 이하. Confluence 문서 평균(아래) 을 보면 문서당 1벡터는 다수 페이지에서 검색 정밀도 손실 위험.

### 2.3 Confluence 페이지 크기 추정 (코드/테스트 단서)

- 코드에서 직접적인 페이지 크기 통계는 없음. 단서:
  - `chunker.py:34-36` 주석: "영문 텍스트에서 chunk_size=512 가 폴백 시 character 512 가 되어 청크가 작아질 수 있으나…" — 페이지가 chunk_size 단위로 여러 개로 쪼개진다는 전제.
  - `extraction_unit.py:6-15` 모듈 docstring: "미니 H4/H5는 부모 H3 아래로 자연 흡수", "거대 단일 섹션은 문단 경계에서 분할" — **실제로 거대 섹션이 있다는 운영 전제**.
  - `processor/pipeline.py:218-227` 로그가 sections/links/code/tables/mentions 개수를 찍음 — 운영 환경에서 다수가 기대됨.
- 정성 추정(일반적 Confluence 사용 패턴):
  - **소형(가이드, FAQ)**: 500~3000 토큰 — 1벡터로 충분.
  - **중형(설계 문서, 회의록)**: 3000~15000 토큰 — 단일 벡터는 무리, 단 ExtractionUnit 단위(1500) 정도면 적정.
  - **대형(아키텍처 문서, 매뉴얼)**: 15000~80000+ 토큰 — 임베딩 한도 초과, 청킹 필수.
- **결론**: 평균/중간값 문서는 ExtractionUnit 1~5개 분량, 대형 문서는 10~50 unit. 문서 단위 1벡터는 대다수 페이지에서 비현실적.

## 3. 문서단위 전환 시 발생하는 일들

### 3.1 임베딩

- **저장량 감소**: 청크 평균 5~10개/문서 가정 시 ChromaDB 엔트리 80~90% 감소. 멀티뷰(body+meta) 까지 합하면 ×2 였으므로 더 큰 감소.
- **임베딩 API 호출 횟수 감소**: ~80% 감소(`pipeline.py:255` 의 `aembed_documents` 한 번에 보내는 배치 크기는 줄지만 호출 자체가 줄지는 않음. 다만 임베딩 비용은 입력 토큰 합산이므로 절감은 거의 0 — **총 입력 토큰은 그대로**).
- **검색 정밀도 손실** (정량):
  - Top-K 검색에서 한 문서가 여러 토픽을 가지면 query 와 한 토픽만 매칭되어도 문서 전체 벡터의 유사도는 평균화되어 K안에 못 듦. 멀티 토픽 문서 비율을 30% 가정 시 recall@10 의 -15~25%pt 손실 추정 (공개 RAG benchmark 의 chunk vs document baseline 격차와 일치).
  - 또한 **멀티뷰(body+meta) 의 의미가 사라짐**: meta 뷰는 "title + section_path" 의 키워드 표면 매칭 보강용(`pipeline.py:426-444`). 문서 단위면 section_path 가 None 이 되어 meta 뷰는 title 만 남고, 사실상 멀티뷰 효과 0.
- **임베딩 한도 위반 위험**: 평균 5K 토큰 문서면 OK 지만 50K 토큰 페이지는 `nomic-embed-text` 가 자동 truncation 하여 **앞부분만 임베딩 → 뒷부분 검색 완전 실패**. ChromaDB/Ollama 가 silent 하게 자르므로 디버깅도 어려움.

### 3.2 그래프 추출

- **결정론 본문 그래프(`body_extractor`)**:
  - 변경 없음 가능 — `build_extraction_units` 가 더 이상 청킹과 결합되지 않으므로 단순화 가능. 단, 거대 문서에서 unit 1개로 응축되면 Relation.label 가 모두 동일해져 그래프 카드의 출처 시그널이 단조로워짐.
  - 정량: 한 unit 에서 도출되는 API/Jira/bold 엔티티 수는 텍스트 길이에 거의 선형. 8K 토큰 unit 한 개와 1.5K 토큰 unit 5개는 같은 엔티티 셋. 단, **section_label 의 정보량이 5배 떨어짐** (모든 관계가 root section path 만 기록).
- **LLM 본문 그래프(`llm_body_extractor`)**:
  - **위험 1 (토큰 한도)**: 위 2.1 에서 봤듯 user content 가 8K 이상이면 응답 예산을 크게 줄여야 하고, 그러면 entities+relations JSON 잘림 → `extract_json` 파싱 실패 → `units_failed` 증가.
  - **위험 2 (single-shot 품질 저하)**: 한 호출에서 보는 본문이 너무 길면 reasoning 모델이 본문 후반부 엔티티를 놓치는 경향(공개 long-context eval). 50K 토큰 user 라면 중반부 엔티티 recall 큰 폭 저하.
  - **이득 (cross-section 관계)**: 현재는 unit 경계를 넘는 관계(예: H2 섹션의 "Auth Service" 와 H4 섹션의 "Token Validator" 의 depends_on) 는 두 unit 에서 각각 등장하지 않으면 잡지 못함. 문서 1 unit 이면 잡힘. 단, 위 위험 1/2 가 더 크다.
  - **현실적 절충**: ExtractionUnit 의 `target_tokens` 를 1500 → 4000~5000 으로 상향하면 cross-section 캐치는 대부분 흡수되면서 안전 마진 유지 가능. 이는 청킹 제거가 아니라 **청크 입자도 통합**.

### 3.3 위치 메타데이터 손실

- 잃는 것:
  - `section_path` (대시보드 청크 탭의 섹션 라벨, 검색 결과 출처 라벨, `_format_chunk_results` 헤더의 `_섹션: …_` — `context_assembler.py:236-239, 413-416`).
  - `section_anchor` (Confluence deep-link 의 URL fragment 용. 현재 `metadata_store` 에 저장되지만 UI 에서 직접 deep-link 를 구성하는 코드는 안 보임. 즉 **저장은 하나 활용은 미흡** — 별도 개선 여지).
  - `section_index` (ExtractionUnit 의 `section_ids` 와 청크의 `section_index` 를 조인하는 키. 그래프 카드에 인용된 섹션을 펼치는 등의 향후 기능이 막힘).
- 영향:
  - **출처 라벨 다운그레이드**: 검색 결과 "[출처: 문서 X] (섹션: A > B)" → "[출처: 문서 X]" 만 남음. 사용자가 어느 부분에서 답이 왔는지 확인이 어려워짐. (5dbf642 / e10b5d7 커밋이 보여주듯 출처/유사도 UX 는 최근 active 한 영역.)
  - **재처리 추적**: section_index 가 unit 의 section_ids 와 매핑되어 "이 그래프 노드는 이 청크에서 왔다" 를 표시할 수 있게 설계됨(`chunker.py:55-58` 주석). 청크 자체가 사라지면 이 조인이 무의미해짐.

### 3.4 출처 표시(citation) 영향

- 현재 citation 단위: **청크**. 검색 결과 1건 = 1 청크 = (문서, section_path) 페어.
- 문서 단위 전환 시: citation = 문서 그 자체. 사용자가 답변의 근거를 확인하려면 전체 문서를 다시 읽어야 함.
- 정량: 청크 1개 = 평균 400~512 토큰 ≈ 한 화면. 문서 평균 5K 토큰 = 10~30 화면. **답변 검증 비용 ~10배 증가**.
- 대안: 문서 단위 임베딩 + 답변 단계에서 LLM 이 "어느 섹션에서 인용했는지" 명시. 단 이건 추가 LLM 호출 비용 + hallucination 위험.

### 3.5 재인덱싱(delete & recreate) 단순화 효과

- 현재: 문서 변경 감지 시 `delete_by_document(document_id)` + `delete_chunks_by_document(document_id)` (이미 document_id 키로 묶여 있음).
- 문서 단위: 동일. **단순화 효과 거의 없음** — 청크가 이미 document_id FK 로 묶여 cascade 삭제가 가능한 구조.
- 청크 수가 줄어 SQLite/ChromaDB 의 delete 비용은 약간 감소하지만 운영적으로 인지 가능한 수준 아님.

## 4. 하이브리드 가능성 분석

### 4.1 옵션 A: 8K 이하 = 1청크, 초과 = 분할 폴백

- **장점**:
  - 소~중형 문서(전체의 60~70% 추정) 는 즉시 1청크 → 인덱싱 메타데이터 단순화, 작은 문서 검색 시 단편화 없는 답변.
  - 대형 문서는 현재 청킹 그대로 → 임베딩 한도 안전.
- **단점**:
  - 임계값 8K 가 어디서 오는가? `nomic-embed-text` 한도. 그러나 512 vs 8K 의 정밀도 격차는 위 3.1 참조 — 한 벡터에 8K 가 들어가면 멀티토픽 문서는 검색 품질 저하.
  - **임계값을 4K~5K 정도로 잡는 게 안전.**
- **구현 변경 규모**: 작음. `chunker.chunk_extracted_document` 의 entry 에서 `count_tokens(extracted.plain_text) <= threshold` 면 single-chunk 분기. **S 공수**.

### 4.2 옵션 B: ExtractionUnit 단위로 통합 (=청크와 unit 단위 일원화)

- **현재 구조의 비효율**: 같은 문서에 대해 `chunker`(512 토큰) 와 `extraction_unit`(1500 토큰) 이 **두 개의 다른 분할 정책** 으로 동시에 실행. 코드/마크다운 블록 보호 로직(`_split_markdown_blocks`)은 두 모듈이 공유하지만 응축/오버랩/breadcrumb 로직은 별도.
- **통합안**: 청크 = ExtractionUnit. 임베딩도 unit 단위로 1 벡터(또는 멀티뷰).
- **장점**:
  - 코드 단순화 (분할 정책 1개로 수렴, `_split_markdown_blocks` 단일 호출 경로).
  - LLM 그래프 추출이 보는 unit 과 검색 결과로 돌아오는 청크가 **일치** → 디버깅 용이.
  - section_path / breadcrumb / section_ids 가 자연스럽게 유지됨.
  - 청크당 ~1500 토큰은 `nomic-embed-text` 정밀도 sweet spot.
- **단점**:
  - 현재 chunk_size=512 정밀도에 의존하던 검색의 recall 분포가 바뀜. 평가 셋 재실행 필요.
  - 마이그레이션: 기존 청크 인덱스 전부 폐기 후 재인덱싱. SQLite chunk_index 자릿수 변동.
- **구현 변경 규모**: 중간. `chunker.chunk_extracted_document` 를 사실상 `build_extraction_units` 의 얇은 래퍼로 만들고, ExtractionUnit → Chunk 어댑터 함수 도입. **M 공수**.

### 4.3 옵션 C: Section 단위(헤딩 1개 = 1청크)

- **장점**:
  - 사람의 정신 모델과 가장 일치. 출처 라벨 = "섹션 X" 가 명확.
  - confluence_extractor 가 이미 sections 를 만들어 두므로 추가 처리 거의 없음.
- **단점**:
  - 섹션 크기 분포가 극단적: 헤딩 본문 없는 빈 섹션, 100 토큰 미니 섹션, 10000 토큰 거대 섹션 공존. **저녁 시간 회의록처럼 H3 가 50개 있는 문서면 50청크** — 청크 수가 오히려 늘어남.
  - 거대 섹션은 여전히 분할 필요 → `extraction_unit._split_oversized` 와 같은 로직 재구현 또는 재사용.
- **구현 변경 규모**: 작아 보이나, 실제로는 거대 섹션 처리 + 미니 섹션 응축이 결국 `extraction_unit` 의 알고리즘을 재현. **옵션 B 와 사실상 수렴**.

### 4.4 chunker 단순화 권고 (옵션 B 선택 시)

- `chunk_extracted_document` 를 다음으로 대체:
  ```python
  def chunk_extracted_document(extracted, *, document_id, doc_title, ...):
      units = build_extraction_units(
          extracted, document_id=document_id, doc_title=doc_title,
          config=ExtractionUnitConfig(target_tokens=1500, max_tokens=4000, ...),
      )
      return [_unit_to_chunk(u) for u in units]
  ```
- `chunk_text`(헤딩 기반 폴백) 는 `confluence_mcp` 가 항상 `extracted.sections` 를 갖도록 보장하면 호출 경로 없어짐 (현재 `pipeline.py:237-243` 의 fallback 은 raw_content 없을 때만). 다른 source_type(upload, manual) 에서 살려야 함.
- 결과적으로 `chunker.py` 의 `_chunk_blocks` 코드(`chunker.py:387-494`) 는 더 이상 confluence 에 쓰이지 않음.

## 5. 정량 추정 요약

| 지표 | 현재 | 옵션 A (8K 임계 hybrid) | 옵션 B (unit 통합) | 옵션 D (문서 단위 강행) |
|------|------|------------------------|--------------------|------------------------|
| 임베딩 호출 횟수 | 100% | 70% | 50% | 10% |
| 임베딩 총 입력 토큰 | 100% | 100% (오버랩만 사라짐, ~5%↓) | 95% | 100% |
| ChromaDB 엔트리 수 | 100% | 60% | 35% | 10% |
| 검색 recall@10 (멀티토픽 문서) | 100% | 95% | 90% | 70%~80% |
| LLM 그래프 추출 안전성 | 100% (1500토큰 unit) | 100% (변경 없음) | 95% (4K unit, 안전 마진 유지) | 50%~70% (한도 충돌, 응답 잘림) |
| 출처 라벨 정밀도 | 청크 단위 | 청크 OR 문서 | unit 단위 | 문서 단위 |
| 코드 복잡도 (LoC) | 100% | 100% | 70% | 90% (메타데이터 정리 보일러플레이트) |

(임베딩 호출 횟수 = `aembed_documents` 호출 1회당 1로 셈. 옵션 B/D 는 청크 수가 줄면서 한 번에 보낼 텍스트 수도 줄어 호출이 늘 수도 있으나, 본질은 텍스트 수.)

## 6. 권고

- **권고**: **옵션 B (청크 = ExtractionUnit 통합)** 채택.
  - 청킹을 제거하는 것이 아니라 **두 개의 분할 정책을 하나로 통합**.
  - target_tokens 를 1500 → 약 3000 으로 살짝 상향(`max_tokens` 5000) 하면 옵션 A 와 옵션 B 의 이점을 모두 흡수.
  - section_path / section_anchor / section_ids 메타데이터 모두 보존, 출처 라벨 UX 유지.
  - LLM 그래프 추출의 cross-section 캐치율도 ExtractionUnit 응축 정책에 의해 자연스럽게 향상.
- **하지 않을 일**: 옵션 D(완전 문서 단위) 강행. `nomic-embed-text` 정밀도 손실, LLM 그래프 추출 토큰 충돌, 출처 라벨 다운그레이드의 세 손실이 동시에 발생.

## 7. 검토하지 않은 영역

- 실제 운영 환경의 페이지 크기 분포 — 코드 단서만 있고 실측 데이터 없음. **이 데이터가 옵션 A vs B 의 임계값 결정의 핵심 인풋**. 별도 분석 또는 운영 로그 수집 필요.
- `nomic-embed-text` 의 한국어/영어 토큰화 효율 차이 — 청크 토큰 카운트가 tiktoken `cl100k_base` 기준인데 실제 임베딩 모델 토크나이저는 다름. 8K 한도가 우리가 세는 토큰으로 몇인지 불일치 가능.
- 검색 단계의 BM25 / hybrid 검색 도입 시 청크 단위가 어떻게 바뀌어야 하는지 — 현재 코드는 pure dense vector. 향후 hybrid 도입 가능성을 가정한 청크 정책은 별도 라운드.
- `git_code` 소스의 청크-AST 단위와 confluence 의 청크-section 단위가 메타데이터 호환성을 유지할 수 있는지 (멀티뷰 dedup 키 등). 동료 분석가(git_code-chunking-analyst) 와 합의 필요.

---

## 문서단위 전환 권고 (이번 라운드 핵심)

- **현재 청킹의 진짜 이유**:
  - `chunker` (512 토큰): **임베딩 정밀도 휴리스틱** (모델 한도가 아닌, "1 벡터가 표현하는 의미 단위" 의 경험적 크기). 코드 근거: `chunker.py:445-453` 의 atomic oversized 허용이 한도-기반이라면 모순임.
  - `extraction_unit` (1500 토큰): **LLM 그래프 추출 호출의 응답 토큰 예산 확보**. 코드 근거: `llm_body_extractor.py:77` 의 max_tokens=32768 주석에 명시.
  - 두 분할은 서로 다른 강제 요인으로 독립 발생 중.
- **문서단위 전환 가능성**: ⚠️ 조건부 가능 — **소형 문서(<3K 토큰)에 한해 1 청크 가능**, 그 외에는 ExtractionUnit 단위 통합이 더 합리적.
- **전환 시 잔여 청킹 필요 케이스**:
  - 임베딩 모델 한도(8192) 초과 문서 — silent truncation 방지를 위해 강제 분할 필수.
  - LLM 그래프 추출 user content 가 5K 토큰 초과 시 — 응답 잘림 / cross-section recall 저하 방지.
  - 다중 토픽 문서(섹션 5개 이상) — 단일 벡터가 멀티 토픽을 평균화하여 검색 recall 저하.
- **권고 전환 방식**: **하이브리드 (옵션 B: 청크 = ExtractionUnit 통합)** 채택.
  - 근거 1: 두 분할 정책의 중복 제거로 코드 LoC 약 -30%.
  - 근거 2: 임베딩 정밀도 sweet spot (1500~3000 토큰) 과 LLM 안전 마진 동시 충족.
  - 근거 3: 기존 메타데이터(section_path/anchor/ids) 무손실 보존, 출처 라벨 UX 유지.
- **예상 영향 (정량 추정)**:
  - 임베딩 호출 ~50% 감소 (청크 수 1/2~1/3).
  - ChromaDB 엔트리 ~65% 감소.
  - LLM 그래프 추출 호출 수 변경 없음 (이미 unit 단위), 단 unit 응축 정책 상향으로 cross-section recall +10~15%pt 추정.
  - 검색 recall@10 변경: 단일 토픽 문서에서 +0~+3%pt (단편화 감소), 멀티 토픽 문서에서 -5~-10%pt (입자도 증가 영향) — 순효과는 평가 셋에 의존, 평가 회귀 테스트 필수.
  - 출처 라벨 정밀도: 청크 단위 → unit 단위 (사용자 인지도 변화 거의 없음, 섹션 정보는 유지).
