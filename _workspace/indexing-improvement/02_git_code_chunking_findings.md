# Git Code Chunking — R2 Findings (심볼 청크 → 파일 청크 전환 검토)

## 요약

- 이번 라운드 핵심 질문: `source_type='git_code'` 의 AST 기반 **심볼 단위 청킹**을 제거하고 **파일 단위 1청크**로 인덱싱할 수 있는가?
- 결론(미리보기): **⚠️ 조건부 가능 — 하이브리드 권고**. AST 추출 자체(`extract_code_symbols` → `to_graph_data`)는 그래프 품질의 핵심이라 반드시 유지. 청킹 함수(`to_chunks`)만 "파일 < 임베딩 한도 = 파일 1청크 / 한도 초과 = 심볼 단위" 형태로 전환 가능. 단 임베딩 한도(8192) 초과 파일이 잔존하므로 청킹 자체를 0으로 만들 수는 없다.
- 새 발견 **6건** (이번 라운드 한정, R1 산출물과 중복 금지):
  - **F-G-R2-01** 심볼 청크가 "강제" 분할 사유는 사실상 **임베딩 모델 입력 한도(8192)**, 그래프/검색 정밀도는 부수 효과
  - **F-G-R2-02** 심볼 단위 메타데이터(`fqn`, `symbol_type`, `line_start/end`)는 **청크 컬럼에 영속화되어 있지 않음** — `chunk.content` 헤더와 `section_path` 로만 표현. 청크 단위 축소 시 손실폭이 예상보다 작음
  - **F-G-R2-03** 한 파일 = 1 임베딩 벡터는 **자연어 ↔ 함수명 정밀 매칭**과 **단일 파일 내 다중 함수 disambiguation** 두 영역에서 RAG 정밀도를 떨어뜨림 (정량 추정 포함)
  - **F-G-R2-04** AST 추출은 그래프(`to_graph_data`) 에 필수 — `to_chunks` 제거하더라도 `extract_code_symbols` 는 유지해야 함
  - **F-G-R2-05** `file_size_limit_kb=500` 기본값과 `nomic-embed-text` 8192 토큰 한도 사이에 큰 갭(약 16배). 거대 파일 자동 분할 정책이 반드시 필요
  - **F-G-R2-06** 하이브리드(파일 임계 미만 = 파일 1청크 / 초과 = 심볼 분할)는 구현 복잡도 작고, 멀티뷰 임베딩(body+meta)·`logical_chunk_id` dedup 구조를 그대로 재사용 가능

---

## 검토 대상 / 환경 사실

| 항목 | 값 / 근거 |
|---|---|
| LLM | `qwen2.5:7b` via Ollama (context 32K, `max_tokens=32768`) |
| 임베딩 모델 | `nomic-embed-text` via Ollama, **8192 토큰** 입력 한도 (공식 spec) |
| `chunk_size` 기본 | 512 토큰 (`PipelineConfig`, `pipeline.py:62`) |
| `file_size_limit_kb` 기본 | **500 KB** (`git_config.py:84`, `config/default.yaml:23`) |
| 본 레포 `.py` 분포 | 최대 ~40KB(상위 ~10K 토큰), 평균은 훨씬 작음 (10K 토큰 이하가 다수) |
| 500KB → 토큰 추정 | 약 125K 토큰 (대략 4 bytes/token) — **임베딩 한도의 ~16배** |
| git_code 청킹 코드 | `ast_code_extractor.py::to_chunks` (line 143-192) — 심볼당 1청크, 헤더 prefix 추가 |
| git_code 파이프라인 분기 | `pipeline.py:131-208` (`if source_type == "git_code":`) — 멀티뷰 임베딩(body+meta), `logical_chunk_id` dedup |
| 청크 영속화 컬럼 | `chunks` 테이블: `id, document_id, chunk_index, content, token_count, section_path, section_anchor, embed_text, section_index` (`metadata_store.py:41-44, 166-180`) — **`symbol_type`/`line_start`/`line_end`/`fqn` 컬럼 없음** |
| MCP/Web에서 line range 노출 | 없음 (`grep`으로 web/, mcp/, storage/ 전영역 0건) |

---

## 발견 사항

### F-G-R2-01 (CORE): 심볼 단위 청크의 "강제 요인"은 임베딩 한도이며, 다른 사유는 결과적 부수 효과

- **위치**: `src/context_loop/processor/ast_code_extractor.py::to_chunks` (143-192)
- **현재 동작**: 심볼(함수/메서드/클래스/모듈 docstring/모듈 상수)마다 1개 `Chunk`를 만들고, 검색용 임베딩 텍스트는 `file_title + parent + name + signature + docstring` 의 식별자 요약을 별도(`meta_texts`)로 생성.
- **분할이 강제되는 진짜 이유 분류**:
  1. **임베딩 모델 입력 한도** — 가장 본질적. `nomic-embed-text` 8192 토큰 초과 파일은 한 번에 임베딩 불가. 심볼 분할로 자연 회피.
  2. **검색 정밀도** — 자연어 질의("토큰 카운트 함수") ↔ 짧은 함수 본문 매칭이 파일 전체보다 코사인 유사도가 높게 나옴 (메타뷰 텍스트가 식별자 중심이라 더 그렇다).
  3. **검색 결과 표시 단위** — 검색 hit 1건이 "한 함수의 본문"이 되어 사용자에게 의미 있게 보임 (파일 전체 hit는 노이즈).
  4. **그래프 입력** — 무관. `to_graph_data` 는 `extraction.symbols` 만 보고 청크에 의존 안 함 (line 210-314).
  5. **토큰 카운트 표시** — 청크당 `token_count` 가 의미 있는 단위로 노출되지만 본질적 강제 요인은 아님.

- **분류 결과**:
  - (1)만이 **회피 불가능한 강제 요인**.
  - (2)(3)은 "심볼 단위가 좋지만 파일 단위로 가도 어느 정도 검색 가능" — 정도의 문제.
  - (4)는 청킹 제거와 무관.
- **함의**: "**파일 < 8K 토큰 = 파일 1청크 / 파일 ≥ 8K = 심볼 청크**" 하이브리드는 (1)을 충족시키면서 (2)(3)의 손실폭을 측정·관리 가능한 수준으로 줄인다.
- **심각도**: Critical (의사결정 근거 자체)

---

### F-G-R2-02 (HIGH): 심볼 단위 메타데이터의 대부분은 청크 컬럼에 영속화되어 있지 않다 — 손실폭이 작음

- **위치**: 
  - dataclass: `ast_code_extractor.py::CodeSymbol` (31-55) — `symbol_type`, `signature`, `line_start`, `line_end`, `docstring`, `parent_name`, `parent_signature`
  - SQLite `chunks` 스키마: `metadata_store.py:36-46, 166-180` — `symbol_type`/`line_start`/`line_end`/`fqn`/`parent_name` **컬럼 없음**
  - ChromaDB metadata: `pipeline.py:164-180` — `document_id, chunk_index, title, section_path, section_anchor, logical_chunk_id, view` 만 저장 (symbol-specific 필드 없음)
- **사실**:
  - `symbol_type`/`signature`/`parent_name` 은 **`chunk.content` 안의 헤더 문자열**(`# File: <title>\n# <parent_sig>\n# <symbol_type>: <signature>\n\n` — line 162-170)로 흡수.
  - `parent_name`/`name` 은 `section_path` (`"<file> > <parent> > <name>"` — line 168, 171)로 흡수.
  - `line_start`/`line_end` 은 dataclass 안에서만 존재. `to_chunks` 시점에 버려짐. 어떤 컬럼/메타데이터에도 저장되지 않음. **검색·표시 어디에서도 사용되지 않음** (web/, mcp/, storage/ grep 0건).
  - `fqn` 은 청크에는 없고 **그래프 노드 이름**으로만 영속화됨 (`to_graph_data` line 256, 270).
- **함의 — 파일 단위 전환 시 손실**:
  - `section_path` 의 ` > parent > name` 꼬리 사라짐 — 그러나 `extract_code_symbols` 를 유지하면 그래프 노드에서 동일 정보 조회 가능.
  - 청크 헤더(`# method: foo(...)`) 사라짐 — 검색 결과 표시에서 "어느 함수인지" 텍스트 단서 약화.
  - `embed_text` (메타 뷰 입력 = 식별자 요약) — 파일 단위에서는 한 파일에 다수 심볼이 있어 단일 요약 텍스트 생성 정책이 필요 (예: 심볼 이름 카탈로그). 후술 F-G-R2-03 참조.
  - line range 손실: **현재도 노출 안 됨 → 손실 0**. 다만 미래 IDE 통합/blame view 시 필요해질 수 있음.
- **심각도**: High (전환 의사결정에 결정적 — 손실폭이 예상보다 작음을 입증)

---

### F-G-R2-03 (HIGH): 파일 1청크가 RAG 정밀도에 주는 영향 — 두 가지 명확한 손실 패턴

#### (a) 자연어 ↔ 함수명 정밀 매칭 약화

- **현재 동작**: 멀티뷰 임베딩. `body` 뷰는 함수 본문(자연어 주석/도메인 용어 포함), `meta` 뷰는 식별자 요약(이름+시그니처+docstring). 두 뷰를 같은 `logical_chunk_id` 로 dedup (`pipeline.py:163-180`, `context_assembler.py:195-207`).
- **파일 단위 전환 시**: 한 파일에 N개 함수가 있어도 임베딩은 1개. 자연어 질의 "토큰 카운트하는 함수는?"에 대해
  - 심볼 청크: `count_tokens` 함수 본문만의 임베딩이 hit → 정확도 ↑
  - 파일 청크: 함수 본문이 파일 전체에 희석된 임베딩이 hit → 같은 distance 점수라도 의미 명확도 ↓
- **정량 추정** (이 레포 기준): 함수 5개 평균인 파일 1청크 vs 5청크 → 임베딩 N건이 1/5로 감소, 그러나 같은 자연어 질의에서 cosine similarity 도 ~5~15% 떨어질 것으로 추정 (single-document dilution; embedding 평균화 효과). 정확한 수치는 평가 시스템(R1 verification report) 으로 측정 필요.

#### (b) 단일 파일 내 다중 함수 disambiguation 약화

- 사용자 질의: "`extract_code_symbols` 함수가 어떻게 동작하나"
- 현재: `extract_code_symbols` 청크가 hit → 그 함수 본문만 반환 (메서드/parent 헤더 포함)
- 파일 단위: `ast_code_extractor.py` 파일 1청크 hit → 전체 파일(~10K 토큰) 반환 → LLM 컨텍스트 낭비 + 사용자 결과 표시에서 "어디?" 불명확
- **완화책**:
  - 파일 단위라도 검색 결과에 "히트 라인 근방 ±30라인" 윈도우를 잘라서 표시 (`extract_code_symbols` 만으로 line range 찾고 후처리 trim)
  - 또는 파일 임베딩과 별도로 "심볼 이름 카탈로그"(`module foo: functions = [extract_code_symbols, to_chunks, ...]`)를 meta 뷰로 추가 임베딩 — 식별자 매칭만 살리고 본문은 파일 단위 유지
- **심각도**: High

---

### F-G-R2-04 (HIGH): `extract_code_symbols` 는 그래프 입력이라 청킹 제거와 무관하게 유지해야 함

- **위치**: `pipeline.py:138, 203` — 한 번 호출된 `extraction` 이 `to_chunks` 와 `to_graph_data` 둘 다에 사용됨.
- **사실 확인**:
  - `to_graph_data` (ast_code_extractor.py:210-314) 가 만드는 그래프 엔티티: `module(파일)`, 각 심볼(`function`/`method`/`class`/`struct`/`interface`), import된 외부 모듈. 관계: `imports`, `contains` (class → method)
  - 그래프는 **검색의 핵심 보강 시그널** — `get_graph_context(entity_name='UserService')` 같은 MCP tool 이 작동하려면 심볼 노드가 필요
  - 파일 단위 청킹으로 전환해도 `extract_code_symbols` 은 **반드시 호출 유지** 해야 그래프 품질이 보존됨
- **함의**:
  - 청킹 전환 비용: `to_chunks` 만 교체 (또는 `to_chunks_unified` 추가) — `extract_code_symbols`/`to_graph_data` 는 무수정
  - AST 파싱 비용은 그래프 때문에 어차피 발생 → **청킹 제거로 인한 CPU 절감 효과는 거의 없음**
  - 절감되는 것은: 임베딩 호출 수, 벡터 저장소 row 수, 청크 row 수, 청크 검색 시 dedup 처리량
- **심각도**: High (전환 설계의 핵심 제약)

---

### F-G-R2-05 (HIGH): `file_size_limit_kb=500` 과 임베딩 8192 토큰 한도 사이의 큰 갭

- **위치**: `git_config.py:84`, `git_repository.py::filter_file` (185-201), `pipeline.py:154` (`embedding_client.aembed_documents(to_embed)`)
- **현재 동작**:
  - `filter_file` 가 500KB 초과 파일을 제외 → 통과한 파일은 최대 500KB ≈ **약 125K 토큰** (코드 기준 ~4 bytes/token).
  - 심볼 청크 단계에서는 한 함수가 8K 토큰을 넘는 경우만 발생 (드뭄) — 자연스럽게 한도 회피.
  - 파일 단위로 임베딩하면 **8K~125K 토큰 사이 파일은 `nomic-embed-text` 입력 한도 초과** → Ollama 호출이 에러 / 토큰 잘림 발생.
- **이 레포 자체 사실 확인**:
  - 최상위 10개 `.py` 파일이 ~20K~40KB (대략 5K~10K 토큰). 임베딩 한도 근접/초과 파일이 이미 존재.
  - 일반 사내 모놀리스 레포(예: `services/billing/handler.go` 등)는 500KB 가까운 파일이 종종 있음 — 거의 항상 한도 초과.
- **함의**:
  - 파일 단위 전환 시 **자동 분할 정책이 반드시 필요**:
    - 옵션 A: 파일 ≥ N 토큰(예: 7000) 시에만 심볼 분할로 폴백 (하이브리드)
    - 옵션 B: 모든 파일을 토큰 기반(`chunk_text` 활용) 청킹 — AST 의미 손실
    - 옵션 C: 임베딩 호출에서 토큰 단위 잘림 허용 (`nomic` 자체가 truncate함) — 의미 손실 + silent failure 위험
  - **권장 옵션 = A**. F-G-R2-04 와 결합: AST 추출은 어차피 한다 → 한도 초과 시 그 결과를 그대로 심볼 청크로 재사용 (코드 중복 없음).
- **심각도**: High (구현 시 필수 안전장치)

---

### F-G-R2-06 (MEDIUM): 하이브리드 구현은 기존 멀티뷰/dedup 인프라를 그대로 재사용 가능

- **위치**: `pipeline.py:131-208`, `ast_code_extractor.py::to_chunks`
- **제안 동작**:
  ```
  extraction = extract_code_symbols(content, title)   # 그래프용 — 항상 호출
  file_token_count = count_tokens(content)
  EMBED_LIMIT = 7000  # nomic-embed-text 8192의 안전 마진
  if file_token_count <= EMBED_LIMIT:
      # 파일 1청크 — body 뷰 = 전체 코드, meta 뷰 = 식별자 카탈로그
      chunks = [single file chunk]
      meta_texts = [f"{title}\n" + symbol_catalog(extraction.symbols)]
  else:
      # 기존 심볼 분할 fallback
      chunks, meta_texts = to_chunks(extraction, title)
  ```
- **구현 비용**:
  - `to_chunks` 시그니처 변경 없이 새 helper `to_file_or_symbol_chunks(extraction, title, content, token_limit)` 추가
  - `pipeline.py` 의 git_code 분기에서 `to_chunks(extraction, title)` → 새 helper 로 교체 (한 줄 변경)
  - 멀티뷰 임베딩(body/meta), `logical_chunk_id` dedup (`context_assembler.py:201`), `section_path` 표시는 모두 **무수정 재사용**
- **부가 이점**:
  - 임베딩 호출 N: 함수 5개 평균 파일 5청크 × 2뷰 = 10건 → 1파일 × 2뷰 = 2건 → **임베딩 호출 ~80% 감소** (한도 미만 파일 기준). 한도 초과 파일은 기존 그대로.
  - 청크 row 수도 비례 감소 → SQLite/ChromaDB 부하 감소
- **부작용 / 측정 필요**:
  - 검색 정밀도 변화 — F-G-R2-03 의 dilution 효과. 평가 시스템(R1 verification report 가 사용한 메트릭) 으로 정량 측정 필요
  - `section_path` 가 파일명만 남음 — "어느 함수의 hit인가" 사용자에게 명시하려면 (a) hit 라인 근방 trim, (b) symbol catalog meta 뷰 의 두 가지 중 하나가 추가 필요
- **심각도**: Medium (구현 가이드)

---

## 검토하지 않은 영역

- 다중 언어(Go/Java/TS/JS) 별 평균 파일 크기 분포 — 본 분석은 Python 위주
- `to_chunks` 가 만드는 헤더(`# File: ...`) 가 임베딩 품질에 미치는 효과의 정량 측정 (현재 헤더가 본문 임베딩에 어느 정도 노이즈인가)
- 거대 single-function 파일(예: 한 함수가 1000라인) — 심볼 단위 청킹조차 한도 초과하는 케이스 (`to_chunks` 도 현재 무방어). 파일 단위 전환과 무관한 별도 이슈.
- 평가 시스템 메트릭 변화 (별도 분석가 영역)
- `confluence_mcp` 의 동일 전환 검토 (별도 분석가 영역)
- 벡터 dedup 시 같은 파일에서 여러 hit(symbol 청크 5개)이 한 사용자 결과에 어떻게 표시되는지 — 파일 단위 전환 시 자연 해소되지만 검색 다양성 영향 있을 수 있음

---

## 문서단위 전환 권고 (이번 라운드 핵심)

- **현재 청킹의 진짜 이유**: 
  - **본질적 강제 = 임베딩 모델 8K 토큰 한도** (`nomic-embed-text`). 코드 근거: `ast_code_extractor.py::to_chunks` 가 심볼 단위 본문을 그대로 임베딩 입력으로 보내고 (`pipeline.py:152-156`), `nomic-embed-text` 8192 토큰 한도 초과 시 silently truncate.
  - **부수 효과** = 검색 정밀도/표시 단위. 본질적 강제는 아니지만 파일 단위 전환 시 측정 가능한 정밀도 손실 발생 (F-G-R2-03).
  - **무관** = 그래프 추출 (`to_graph_data` 는 `extraction.symbols` 만 보고 청크 비의존).

- **문서단위 전환 가능성**: ⚠️ **조건부 가능 (하이브리드 권고)**
  - **불가능한 경우**: 파일 ≥ 임베딩 8K 토큰 한도. 잔존 청킹 불가피.
  - **가능한 경우**: 파일 < 8K 토큰 (이 레포 기준 절대 다수). 파일 1청크로 전환 가능.

- **전환 시 잔여 청킹 필요 케이스**:
  1. 한 파일이 임베딩 모델 입력 한도(`nomic-embed-text` 8192) 초과 — 자동 폴백
  2. 한 단일 심볼(함수)이 한도 초과 — 매우 드물지만 무방어 상태 (별도 개선 필요, 본 라운드 범위 밖)
  3. R1 에서 제외되지 못한 거대 vendored/generated 파일이 남아있는 케이스 — `_DEFAULT_EXCLUDED_DIRS` (R1 F-G-13) 가 적용되지 않은 일부 자동 생성 파일(`.pb.go`, `.gen.ts`)

- **권고 전환 방식**: **하이브리드 (`to_chunks` 미제거, 동작 분기)**
  - **근거 1**: 임베딩 한도(F-G-R2-05) 가 파일 단위 단독 전환을 막음. 완전 제거는 불가.
  - **근거 2**: AST 추출(`extract_code_symbols`)은 그래프 때문에 어차피 호출 — 한도 초과 파일에 대한 심볼 분할 폴백은 추가 비용 0 (F-G-R2-04).
  - **근거 3**: 멀티뷰(body+meta) 임베딩과 `logical_chunk_id` dedup 구조가 파일/심볼 두 단위 모두에 그대로 작동 (F-G-R2-06).
  - **근거 4**: 영속화된 청크 메타데이터(`chunks` 컬럼)의 손실폭이 작음 — `symbol_type`/`line_start`/`line_end` 은 어차피 검색·표시에 미사용 (F-G-R2-02). `section_path` 의 ` > parent > name` 꼬리만 손실, 이건 그래프 노드로 보강 가능.
  - **구현 트리거**: 파이프라인 `git_code` 분기에서 `count_tokens(content)` 임계 비교 → `to_chunks(extraction, title)` 또는 신규 `to_file_chunk(extraction, title, content)` 분기. 코드 변경 한 줄 수준.

- **예상 영향 (정량 추정)**:
  - **임베딩 호출 감소**: 한도 미만 파일(이 레포 기준 ~80~95% 추정)에 대해, 평균 5심볼 → 1청크 = **호출 80% 감소**. 멀티뷰 2x 곱하면 절대 호출 수가 N×2 → 1×2 로 떨어짐.
  - **벡터 row 감소**: 동일 비율. ChromaDB index 메모리/디스크 ~80% 감소.
  - **검색 정밀도 변화**: dilution 영향으로 자연어 정밀 매칭 cosine similarity 5~15% 감소 추정. 평가 시스템으로 측정 필요. `symbol catalog` meta 뷰 추가 시 손실 일부 회복 가능.
  - **LLM 호출 변화**: 0건. (`process_document` 의 git_code 분기에는 LLM 호출 없음 — `pipeline.py:131-208`, 모두 결정론적)
  - **그래프 품질**: 변화 없음 (F-G-R2-04).
  - **저장 공간**: 청크 content 총량은 같으나 row 수가 줄어 SQLite/ChromaDB 인덱스 오버헤드 감소 (~10~20% 추정).

- **잠정 결정 트리** (구현가에게 전달용):
  ```
  파일 content 토큰 수 측정 (count_tokens)
  ├── ≤ 7000 토큰  → 파일 1청크 (body: 전체, meta: symbol catalog)
  └── > 7000 토큰  → 기존 to_chunks() 심볼 분할 fallback
  ```
  임계값 7000은 8192 한도의 안전 마진 (헤더 추가/특수 토큰 고려). 설정으로 노출 권장 (`processing.embedding_token_limit` 또는 유사).

