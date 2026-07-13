# Git Code Graph — 문서단위 추출 전환 영향 분석 (R2)

> R1 산출물(`_workspace/indexing-improvement_prev_R1/04_git_code_graph_findings.md`)의 11건은
> 모두 그대로 유효하다. 본 보고서는 R2 스코프(“청크 → 문서/파일 단위 인덱싱” 전환)에
> 한정해 **그래프 추출** 측 영향과 가능성을 정리한다. 핵심 결론을 먼저 요약하고,
> R1 미해결 항목(F-GG-03 상속/구현, F-GG-04 call graph) 해소 가능성을 본다.

## 한 줄 요약

git_code 의 그래프 추출은 이미 “파일 단위(AST 결정론)” 로 동작하고 있으며 청크와
독립적이다. 청킹 제거는 그래프 추출에 **아무 손해도 주지 않으며**, 오히려
파일 전체를 LLM 에 넣을 수 있는 길을 여는 부수효과로 R1 의 F-GG-03 / F-GG-04
(상속/호출 그래프)를 **조건부로 해결 가능**하게 만든다.

---

## 1. 현재 코드 그래프 추출 단위 — 코드 근거

### 1.1 git_code 경로는 AST 결정론만 사용 (LLM 미사용)

`src/context_loop/processor/pipeline.py:131-208` 의 `if source_type == "git_code":`
블록은 다음만 호출한다:

- `extract_code_symbols(content, title)` — `ast_code_extractor.py:103`
- `to_chunks(extraction, title)` — `ast_code_extractor.py:143` (벡터/청크 저장)
- `to_graph_data(extraction, title)` — `ast_code_extractor.py:210` (그래프 저장)

그리고 `else` 브랜치(Confluence/upload)에만 다음이 있다:

- `extract_body_graph(units, ...)`           (`pipeline.py:331`)
- `extract_llm_body_graph(units, ..., llm_client=llm_client)` (`pipeline.py:353`)

즉 **git_code 는 LLM 그래프 추출이 호출되지 않는다.** 분기는 source_type 기준으로
완전히 갈라져 있으며, `cfg.enable_llm_body_extraction=True` 기본값에도 불구하고
git_code 는 그 영향 밖이다. `extraction_unit.py:328` 의 `build_extraction_units` 도
`ExtractedDocument.sections` (Confluence HTML 트리) 를 입력으로 받으므로 코드 파일에는
적용되지 않는다.

### 1.2 AST 추출의 “단위”

`extract_code_symbols(content, file_path)` 는 **파일 1개의 content 전체** 를 받아
한 번에 파싱한다 (`ast_code_extractor.py:103-140`). Python 은 `ast.parse(content)`
한 번(line 342), brace 언어는 `splitlines + 라인 스캔` 한 번. 라인 단위 부분 파싱이나
청크 단위 호출은 어디에도 없다.

그래프 산출도 마찬가지로 한 파일의 `CodeExtraction` 객체 1개를 받아
`to_graph_data` 가 한 번 호출되어 (`pipeline.py:203`) entity/relation 리스트를
GraphStore 에 흘려보낸다.

### 1.3 결론

- **현재 코드 그래프 추출 단위 = 파일 단위 (이미 “문서단위”)**
- **현재 그래프 추출은 결정론, LLM 미호출**
- R1 미해결 F-GG-03 (상속/구현) / F-GG-04 (call graph) 는 *AST 추출기에 로직을
  추가하지 못해서가 아니라*, 정규식 / `ast.bases` / `ast.walk(Call)` 를 아직
  적용하지 않아서 비어 있는 상태. 청킹과는 인과 관계가 없다.

---

## 2. AST 추출은 청킹과 완전히 독립이다 — 코드로 입증

### 2.1 파이프라인 호출 그래프

```
extract_code_symbols(content, title)   # 입력: file content 전체
    │
    ├── to_chunks(extraction, title)   # 청크 출력 → vector_store
    │
    └── to_graph_data(extraction, title)  # 그래프 출력 → graph_store
```

두 호출은 **같은 `extraction` 객체** 를 공유하지만, 서로 데이터를 주고받지 않는다.
청크가 0개여도 그래프는 정상 생성되고, 그 반대도 성립한다.

`pipeline.py:140-208` 에서 `if chunks:` 블록(141-200) 과 `if graph_data.entities:`
블록(204-207) 은 동등 레벨로 나란히 놓여있고, 어느 한쪽 실패가 다른 쪽을 막지 않는다.

### 2.2 storage 스키마에서의 검증

`storage/metadata_store.py:47-62` — `graph_nodes`, `graph_edges` 두 테이블 모두
`document_id` 만 외래키로 가진다.

```sql
CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    entity_name TEXT NOT NULL,
    entity_type TEXT,
    properties TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    source_node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relation_type TEXT,
    properties TEXT
);
```

`chunk_id` 컬럼 / FK 가 **존재하지 않는다**. `chunks` 테이블 (`metadata_store.py:35-45`)
역시 `document_id` 를 가질 뿐 graph_nodes 쪽으로 향하는 참조는 없다. 양쪽 모두
`documents.id` 를 허브로 쓰는 별-스키마.

`graph_node_documents` (64-68) 도 노드↔문서 다대다일 뿐 청크와 무관.

### 2.3 GraphStore 의 노드 키

`storage/graph_store.py:163-167` — 노드 병합 키는 `(entity_name, entity_type)`
(대소문자 무시). 청크 ID 가 절대 들어가지 않는다.

```python
existing = await self._store.find_graph_node_by_entity(
    entity.name, entity.entity_type,
)
```

R1 의 F-GG-05 (chunk section_path 와 graph entity_name 포맷 불일치) 는 “검색 단계의
hybrid 결합” 문제이지 “그래프가 청크에 의존한다” 는 의미가 아니다.

### 2.4 결론

- 그래프 추출 / 저장 어디에도 chunk_id 참조 없음 — 스키마/코드 모두에서 검증.
- 청킹을 0 으로 줄여도 그래프는 그대로 생성 / 저장 / 조회 가능.
- 마이그레이션 부담 없음 (스키마 변경 불필요).

---

## 3. 파일 전체를 LLM 에 넣어 의미 분석 — 가능성 검토

### 3.1 현재 누락된 시그널 (R1 미해결분)

- **F-GG-03 (Critical)**: 클래스 상속/구현 (`class Foo(Base)` / `extends` /
  `implements`) — 그래프 부재. AST 만으로도 잡을 수 있는 신호인데 아직 미구현.
- **F-GG-04 (High)**: 함수 호출 그래프 (`calls`) — AST `ast.walk(Call)` 로 어림은
  가능하지만 외부 / 같은 파일 / 모듈 간 호출 구분이 휴리스틱.
- 디자인 패턴, 책임 분리, “이 모듈의 역할” — AST 가 못 잡는 의미 시그널.

### 3.2 AST 만으로 해결 가능 vs LLM 통합 호출이 필요한 항목

| 항목 | AST 만으로 해결 가능 | LLM 통합 호출의 이득 |
|------|--------------------|---------------------|
| F-GG-03 상속/구현 | **가능** (Python `node.bases`, brace `extends/implements` 정규식) | 거의 없음 — AST 로 충분 |
| F-GG-04 같은-파일 함수 호출 | **가능** (정의 심볼 집합 + `ast.Call` 매칭) | 어림짐작 정밀도 보강 |
| 모듈 간 호출 (`foo.bar()` → `foo` 의 import + bar) | **부분 가능** (import alias 추적 필요, F-GG-10) | 의미 의도까지 LLM 이 라벨링 |
| 디자인 패턴 (Factory/Strategy/Adapter) | **불가능** | **고유 이득** |
| “이 모듈의 책임” / 의미 라벨 | **불가능** | **고유 이득** |
| 의미 관계 (`depends_on`, `implements (개념적)`) | **부분** | confluence 측 `llm_body_extractor` 와 동일한 어휘로 코드 그래프에 추가 가능 |

요점: **F-GG-03 은 AST 강화로 충분히 해결.** F-GG-04 의 핵심(같은-파일 호출)도
AST 로 충분하며, 모듈 간 호출의 정밀도는 LLM 보조가 가치 있다. **LLM 호출의
진짜 신가치는 “패턴/책임/의미 관계”** 이고, 이는 청킹 제거와는 별개의 기능 추가
결정이다.

### 3.3 파일 토큰 수 LLM 한도 적합성

- 환경: `qwen2.5:7b`, num_ctx override 32K (`_round_scope.md`)
- 본 리포의 `src/context_loop/processor/` 파일들 토큰 수 추정:
  - 평균: 1K~5K 토큰 (Python source 200~700 LOC 기준)
  - 큰 파일: `pipeline.py` ≈ 4K, `ast_code_extractor.py` ≈ 9K, `graph_store.py` ≈ 8K
  - 한도 초과 위험은 자동생성 코드(예: protobuf, lockfiles) 또는 대형
    legacy monolith (>2000 LOC, 30K+ 토큰)
- 추정: 일반 사내 코드베이스의 **95%+ 파일이 32K 안에 한 번에 들어간다.**
  거대 파일은 “함수 단위 윈도우 + 파일 메타 prefix” 의 폴백으로 처리하면 됨.
- 임베딩 `nomic-embed-text` (8K) 한도는 별도 문제. 코드는 임베딩 입력으로 *심볼
  단위 meta 텍스트(`to_chunks` 의 `meta_texts`, ast_code_extractor.py:184-190)*
  를 쓰므로 8K 와 거리가 멀어 영향 없음 — 청킹 제거가 임베딩 한도를 건드리지 않는다.

### 3.4 비용 / 속도 / 그래프 풍부도 trade-off

LLM 통합 호출을 *추가* 한다고 가정 (현재는 git_code 에 LLM 호출 0건):

| 지표 | 현재 (AST only) | + 파일 단위 LLM 1회 |
|------|----------------|---------------------|
| 그래프 추출 LLM 호출 수 | 0 / 파일 | 1 / 파일 |
| 평균 응답 시간 | <50ms (AST) | +2~10s (Qwen2.5 7B 로컬) |
| 그래프 풍부도 | 함수/클래스/메서드/import/contains | + inherits + (선택) calls + depends_on/implements/uses 라벨 |
| 그래프 노이즈 위험 | 매우 낮음 | 중간 (LLM hallucination — 코드에 없는 호출/관계 생성) |
| 결정론성 | ✅ | ❌ (temperature 0.0 도 모델 업데이트 시 변함) |

확장 시 권고: `llm_body_extractor` 와 동일하게 **min_token gate**, **결정론 추출
결과 검증** (LLM 이 만든 엔티티가 AST 심볼 집합에 있는지 확인) 로 노이즈 억제.

---

## 4. storage 영향 — 구체

### 4.1 graph_store / graph_nodes / graph_edges

- chunk_id 의존 0건 (§2.2). 청킹 제거 시 스키마 변경 불필요.
- `save_graph_data` (graph_store.py:137) 는 `(document_id, GraphData)` 입력. AST
  추출이 파일 단위로 1회 호출되든, LLM 이 같은 입력으로 1회 더 호출되든,
  `link_graph_builder` + `body_extractor` + `llm_body_extractor` 처럼 다중 추출기가
  같은 document_id 로 누적 저장하면 자연 병합 (Confluence 측 패턴 그대로 복제 가능).

### 4.2 chunks 테이블의 정체

`chunks` 테이블은 청크 텍스트와 임베딩 키 (vector_store 의 ID) 매핑에 쓰일 뿐
그래프와 무관. 만약 “청크 제거 = 파일 단위 단일 chunk” 로 정의해도, graph_nodes /
graph_edges 는 영향이 없고 vector_store 만 영향을 받는다 (→ chunker 분석가 영역).

### 4.3 entity_name 의 FQN 형식 영향

청킹 제거 자체는 FQN 형식과 무관. 단, R1 의 F-GG-05 (chunk section_path 와 graph
entity_name 포맷 불일치) 의 해결은 청킹 정책 결정 시 자연스럽게 같이 고민할 수
있다 — chunk 가 1개/파일이 되면 section_path 가 의미를 잃기 때문.

---

## 5. 검색 영향

### 5.1 “함수 X 가 Y 를 호출한다” 류 질의의 의존성

- 현재 `graph_search_planner.py` 는 `graph_store.get_neighbors(entity_name, depth)`
  를 호출 (`graph_search_planner.py:384, 408, 431`).
- `get_neighbors` (`graph_store.py:339-421`) 는 노드 이름(FQN / scoped / short)
  표면 매칭 → 양방향 BFS 로 이웃 반환. 청크와 무관.
- 즉 “X가 Y를 호출한다” 는 **그래프 엣지 (`calls`) 존재 여부** 에 달려있다. 청크가
  사라져도 엣지가 있으면 정답이 나오고, 청크가 있어도 엣지가 없으면 정답이 안 나옴.
- **결론: 청크 없이 그래프만으로 “함수 호출 관계” 질의 응답이 가능하다 — 단, F-GG-04
  (call graph 부재) 가 해결된 이후.**

### 5.2 청크의 역할 차이

코드 검색의 두 시그널:

1. **그래프 노드/엣지** — “심볼이 무엇이고 어떻게 연결되어 있나”
2. **벡터 청크** — “그 심볼의 본문 / 자연어 설명 / docstring 으로 매칭”

청킹을 제거해서 “파일 1개 = chunk 1개” 로 합치면 ②번의 retrieval grain 이 거칠어진다:

- 큰 파일에서 특정 메서드 본문에 있는 한국어 주석 (“카드 결제 실패 시 재시도 정책”)
  으로 매칭할 때, body 가 파일 전체이면 임베딩이 평균화되어 매칭 신호 희석.
- 반면 그래프 traversal 결과로 “관련 심볼 노드” 가 나오면, 그 노드의
  `properties.description` (signature) 으로 거꾸로 청크/파일 위치를 찾을 수 있다.

즉 청크가 사라지면 **그래프의 entity description 풍부도** 가 더 중요해진다 —
LLM 통합 호출이 들어오는 경우 entity description 에 “책임 요약 한 줄” 같은 의미
라벨을 LLM 이 채우는 가치가 커진다.

### 5.3 검색 품질 추정

- AST-only 그래프 + 파일-단위 chunk(=청킹 제거) 조합:
  - “함수 X 의 시그니처는?” 류 — 그래프 description (signature) 에 있어 OK
  - “API 핸들러는 어떤 검증 로직을 호출하나?” — F-GG-04 미해결이면 답 불가
  - “결제 실패 재시도 정책이 어디에 구현되어 있나?” — 청크 grain 거칠어져 정확도 ↓
- AST + 파일-단위 LLM 통합 호출 그래프:
  - 위의 첫 두 케이스 모두 그래프에서 직접 답이 나옴
  - 세 번째 케이스도 LLM 이 entity description 에 “재시도 정책 구현” 같은 라벨을
    심으면 파일 단위 chunk 매칭이 그래프 hit 으로 보강됨

---

## 6. R1 F-GG-03 / F-GG-04 해소 가능성

| R1 항목 | R2 “파일단위 LLM 호출” 도입 시 해소? | 비고 |
|---------|-------------------------------------|------|
| F-GG-03 (상속/구현, Critical) | ✅ 가능 — 단, **AST 강화만으로도 충분**. 파일을 LLM 에 넣지 않아도 됨. 청킹 제거가 직접 기여하지는 않음. | Python `node.bases`, brace `extends/implements` 정규식 추가 |
| F-GG-04 (call graph, High) | ⚠️ 부분 해소 — 같은-파일 호출은 AST 로 충분. 모듈 간 호출은 LLM 통합 호출이 정밀도 보강. | LLM 보조의 진짜 이득 영역 |
| F-GG-01 stdlib/third-party 구분 | ⚠️ 부분 — heuristic + LLM 라벨 가능 | LLM 호출 이득 작음 |
| F-GG-02 from-import symbol | ✅ 이미 R1 후 코드에 적용됨 (`ast_code_extractor.py:79, 119, 462-475`) | R1 권고 반영 완료 |
| F-GG-09 Go imports 오탐 | ✅ 이미 R1 후 코드에 적용됨 (`_extract_go_imports`, 870-883) | R1 권고 반영 완료 |
| F-GG-11 graph_vocabulary 코드 타입 누락 | ✅ 이미 R1 후 코드에 적용됨 (`graph_vocabulary.py:60-67`) | R1 권고 반영 완료 |
| F-GG-05 FQN ↔ section_path 불일치 | 청킹 제거 시 section_path 가 의미 잃음 — 다른 식의 통일 필요 | hybrid 검색 join 키 재정의 |

**핵심 메시지**: 청킹 제거(R2의 동기) 자체로는 F-GG-03/F-GG-04 가 자동으로 풀리지
않는다. 그러나 청킹 제거와 동시에 채택되는 “파일 단위 LLM 통합 호출” 옵션은
F-GG-04 의 모듈 간 호출 / 의미 관계 보강에 결정적 이득을 준다. F-GG-03 은
청킹/LLM 과 무관하게 AST 강화 한 번으로 끝낼 수 있는 작업이다.

---

## 7. 잔여 위험 / 주의점

1. **자동생성 코드 / 거대 단일 파일** (예: protobuf, OpenAPI generator, 10K+ LOC
   legacy) — 32K 토큰 한도를 넘는 경우. → 파일 단위 LLM 호출에 “토큰 가드 + 함수
   윈도우 분할” 폴백 필요 (현 `llm_body_extractor._gate_units` 패턴 복제 가능).
2. **LLM hallucination 한 함수/심볼** — 코드에 존재하지 않는 메서드/클래스를 LLM 이
   relation 의 source/target 으로 만들 위험. AST 심볼 집합으로 **post-filter**
   필수 (현 `llm_body_extractor` 의 `unit_valid_entity_names` 검증 패턴과 동일).
3. **결정론성 손실** — AST-only 는 입력이 같으면 출력이 같다. LLM 도입 시 reindex
   결과가 모델 패치마다 달라질 수 있어 골든셋 평가 변동 폭이 커진다.
4. **비용** — 자체 Ollama 호스팅이라 단가 0 에 가깝지만, 큰 레포(1만 파일+)에 파일당
   1회 호출 시 인덱싱 시간이 *시간 단위* 로 증가 가능. R1 의 비용 게이팅
   (`min_unit_tokens`, `skip_split_overlap_parts`) 을 코드용으로 재해석:
   - “심볼 < 5개” 파일 (예: `__init__.py`) 스킵
   - 자동생성 파일 (헤더 주석으로 식별) 스킵

---

## 문서단위 전환 권고 (이번 라운드 핵심)

- **현재 청킹의 진짜 이유 (그래프 측면)**: 그래프 추출은 청킹과 완전히 독립이다.
  현재 청킹이 존재하는 이유는 **벡터 검색의 retrieval grain** (메서드/함수 단위
  매칭) 때문이지, 그래프 LLM 입력 한도 때문이 아니다. git_code 그래프는 이미
  파일 1개를 한 번에 AST 로 파싱하여 결정론적으로 추출한다
  (`ast_code_extractor.py:103, 210`, `pipeline.py:131-208`).

- **문서단위 전환 가능성**: ✅ **그래프 측면에서는 무조건 가능**. 스키마 변경
  불필요, 코드 변경 불필요. graph_nodes / graph_edges 는 chunk_id 의존 0건
  (`metadata_store.py:47-62`).

- **전환 시 잔여 청킹 필요 케이스 (그래프 한정)**: 없음. 그래프 추출은 청크 grain
  을 요구하지 않는다. (참고: 벡터 측면의 잔여 청킹 케이스는 chunker 분석가
  영역.)

- **권고 전환 방식**: **그래프 측은 “미전환”** — 이미 파일 단위. 추가 동작 없음.
  단, R2 변화의 부수효과로 다음 옵션이 의미를 갖는다:
  - **옵션 A (저비용)**: AST 강화만 (F-GG-03 상속/구현, F-GG-04 같은-파일 calls).
    LLM 호출 0건 유지. 결정론성 유지.
  - **옵션 B (고이득, 권장 다음 단계)**: AST + 파일 단위 LLM 1회 통합 호출 도입.
    `llm_body_extractor` 의 git_code 변종을 만들어 도메인 의미 관계 (`depends_on`,
    `implements`, `calls`, `provides`) 와 모듈 책임 라벨을 그래프에 보강. 결정론
    그래프와 같은 document_id 로 누적 저장 → GraphStore 자연 병합.
  - 옵션 B 는 “청킹 제거” 결정과 직교한다. 청킹 제거가 채택되든 안 되든 독립적으로
    가치가 있다.

- **예상 영향 (정량 추정)**:
  - 그래프 LLM 호출: 현재 0건 → (옵션 B 채택 시) 파일 수만큼 (예: 5천 파일 레포
    = 5천 호출). Ollama 로컬 호스팅 기준 인덱싱 시간 2~10 시간 증가.
  - 그래프 노드/엣지 수: 옵션 A 로 F-GG-03/04 해소 시 엣지 +30~50% (상속·calls 보강).
    옵션 B 추가 시 의미 라벨 description 보강 + 도메인 관계 +10~20%.
  - 검색 정밀도: “함수 호출 관계” / “상속 구조” 질의의 정답률이 0 → 본격 응답 가능
    수준으로 step-change. 자연어 의미 질의 (“이 모듈의 책임”) 는 옵션 B 에서만 의미.
  - F-GG-03 해소: 옵션 A 단독으로 가능 (Critical → 해결).
  - F-GG-04 해소: 옵션 A 로 같은-파일 한정 부분 해소, 옵션 B 로 모듈 간 호출까지 확장.

---

## 8. chunking-analyst 와의 충돌 / 공유점

- 같은 파일 (`ast_code_extractor.py`) 을 보지만 영향 범위가 다르다:
  - chunking 분석가: `to_chunks` (143-192) 와 vector_store 경로
  - graph 분석가(본 보고서): `to_graph_data` (210-314) 와 graph_store 경로
- **공유 영역**: `CodeExtraction` 객체와 `_symbol_fqn` / `_class_fqn`. 청킹 제거
  결정이 “심볼 단위 → 파일 단위” 로 가는 경우, `to_chunks` 의 `section_path`
  포맷이 변할 수 있는데 (`{file} > {parent} > {name}` → `{file}`), 이는 F-GG-05
  (FQN vs section_path 통일) 의 자연스러운 해소 경로가 된다.
- **충돌 없음**: 그래프 측은 청킹 변화에 무관하므로 chunking 분석가의 어떤
  결정에도 그래프 영역에서의 반대 신호를 보내지 않는다.
