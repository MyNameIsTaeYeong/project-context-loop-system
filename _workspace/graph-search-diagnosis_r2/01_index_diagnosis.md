# Graph Index Diagnosis (R2 — 실제 DB)

> R1은 정적 분석만 했음. R2는 실제 운영 DB(`/Users/ty/.context-loop/data/metadata.db`)와 코드 시뮬레이션 기반.

## 인덱스 현황 (실측)

| 항목 | 값 |
|------|----|
| 총 노드 | 21 |
| 총 엣지 | 16 |
| 그래프 데이터 보유 문서 | 1개 (document_id=5) |
| 전체 문서 | 4개 (id=1 chunk, id=2/4 pending, id=5 graph) |

### entity_type 분포
- service: 6 (Auth Service, Order Service, Product Service, Payment Service, Notification Service, Search Service)
- team: 6 (KakaoPay, Toss PG사, 플랫폼 팀, 커머스 팀, 결제 팀, CX 팀)
- component: 5 (PostgreSQL DB, Redis, MySQL DB, " 결제 DB (PostgreSQL)", Elasticsearch)
- system: 4 (API Gateway, Kafka, SMTP 서버, FCM)

### relation_type 분포
- uses: 7
- depends_on: 6
- publishes_to: 2
- consumes_from: 1

## F-IDX-R2-01 (CRITICAL): 그래프 방향성 — 6/21 노드만 outgoing edges 보유

| 노드 유형 | outgoing-있음 | outgoing-없음(sink) |
|-----------|--------------|-------------------|
| service | 6 | 0 |
| 나머지 | 0 | 15 |

**sink 노드 (15개)**: PostgreSQL DB, Redis, Product Service, MySQL DB, Kafka, KakaoPay, Toss PG사, " 결제 DB (PostgreSQL)", SMTP 서버, FCM, Elasticsearch, 플랫폼 팀, 커머스 팀, 결제 팀, CX 팀.

- **gold 후보 subgraph 생성 영향**: `load_candidate_subgraphs`가 `get_neighbors(name, depth=1)`로 후보를 필터 (`min_neighbors=1` 디폴트). `get_neighbors`가 outgoing만 따라가서 sink 노드는 영원히 gold seed가 되지 못함 → gold 질문은 6개 entity만 다룸.
- **검색 측 영향**: 이게 실제 funnel 손실의 결정적 원인 (자세히는 `02_search_pipeline_diagnosis.md` 의 F-SRCH-R2-01 참조).

## F-IDX-R2-02 (HIGH): entity_name 데이터 품질 — leading space

- node id 12: `" 결제 DB (PostgreSQL)"` — entity_name 앞에 공백 1개
- 영향: T1/T2/T3 매칭은 양쪽 `.strip()` 으로 회복 가능 (graph_match.py:271, 277, 300). 그러나 LLM 추측 → get_neighbors 매칭(graph_store.py:339-358)은 `.lower()`만 하고 `.strip()` 없음 → LLM이 공백 없이 "결제 DB (PostgreSQL)"를 답하면 표면 매칭 실패. 임베딩 fallback이 회복은 가능하나 호출 절약 차원에서 인덱싱 시 데이터 정제 권고.
- 추출 로직 영역 (`indexing-improvement` 하네스) — 본 하네스 범위 외이나 발견은 등록.

## F-IDX-R2-03 (MEDIUM): R1 임베딩 fallback 의존 — entity_embeddings 캐시 휘발성 미해결

- R1은 `get_neighbors` / `execute_graph_search` 의 임베딩 fallback을 추가했지만 캐시 자체의 영속성/락은 미해결 (R1 보고서에서 R2 후보로 명시).
- 평가 시 `eval_search.py:880-887` 가 pre-build로 1회 명시 호출 → 평가 컨텍스트에서는 race 위험 ↓. 그러나 운영 MCP 서버에서 동시 요청 race 위험 잔존.
- 캐시 무효화 패턴(`self._entity_embeddings.clear()` in `load_from_db:130`)도 재로드 비용 큼.

## F-IDX-R2-04 (LOW): multi-edge 미발생 (R1 F-IDX-01 deferred 가설 — 현재 데이터 무영향)

- 실측: 16개 엣지의 `(src, tgt)` 페어 모두 단일 relation_type — multi-edge 0건. NetworkX DiGraph 의 multi-edge 손실 가설은 현재 데이터로는 발현되지 않음.
- R1에서 R2로 미룬 MultiDiGraph 전환은 우선순위 ↓로 조정 권고. 데이터가 풍부해진 후 재검토.

## 인덱스 vs gold 매칭 가능성 (인덱스 측 검토)

- gold의 `relevant_graph_entities[0].name` 은 `load_candidate_subgraphs` 의 `sg["entity_name"]` 그대로 (build script line 977: `name=sg["entity_name"]`) → **인덱스 노드명과 글자 단위 동일**.
- 따라서 gold-side ↔ retrieved-side의 surface key 정합성은 OK. 문제는 **retrieved가 gold-seed를 포함하지 못하는 funnel**.
- LLM 추측 entity_name 이 sink 이웃을 가리키면, retrieved 가 sink 자신만 담아 gold-seed를 누락. R1 임베딩 fallback은 surface 매칭 실패 시에만 발동 — sink 표면 매칭 성공 → fallback skip → gold-seed 누락.

## 권고

| ID | 권고 | 우선 |
|----|------|------|
| F-IDX-R2-01 | get_neighbors 양방향 traversal (search 측에서 처리) | **Critical** |
| F-IDX-R2-02 | save_graph_data에서 entity_name `.strip()` 정제 | Medium (indexing-improvement) |
| F-IDX-R2-03 | entity_embeddings 영속화 + lock | Medium (R3 후보) |
| F-IDX-R2-04 | MultiDiGraph 보류 | Low |
