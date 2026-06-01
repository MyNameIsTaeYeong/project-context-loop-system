# 설계(아키텍처) 업무 자동화 시나리오 설계

> 분석/시나리오 설계 문서. 구현 아님. 코드 기준: `origin/main` 워크트리(`/tmp/clp-origin-main`).
> project-context-loop-system 의 RAG + **그래프** 역량을 설계 업무 자동화에 적용하는 시나리오를 도출한다.

## 0. 전제: 시스템 핵심 자산 요약

설계 자동화의 토대가 되는 building block 을 코드 기준으로 정리한다.

| 자산 | 위치 | 설계 자동화 관점 의미 |
|------|------|----------------------|
| 코드 심볼/import/contains 그래프 | `processor/ast_code_extractor.py` `to_graph_data()` (L210~314) | 파일=`module` 노드, 심볼=FQN(`file.py::Class.method`), `imports`/`contains` 관계. **AST 정적 추출이라 정확(D-036)** |
| 문서 링크 그래프 | `processor/link_graph_builder.py` | Confluence 페이지 간 `references`, `mentions_user`, `mentions_ticket`, `has_attachment`. 결정론적(LLM 無) |
| 문서 본문 의미 그래프 | `processor/llm_body_extractor.py` + `graph_vocabulary.py` | `depends_on`/`implements`/`calls`/`owned_by`/`supersedes` 등 의미 관계. **LLM 추출이라 품질 이슈 이력 있음** |
| 그래프 저장/탐색 | `storage/graph_store.py` (`get_connected_component` L452, `get_neighbors` L526, `get_edges_between` L607, `stats` L628, `get_schema_summary` L637) | NetworkX DiGraph + SQLite. hop 거리 BFS, 임베딩 fallback 시드 매칭 |
| 엔티티 정규화/머지 | `storage/entity_normalizer.py` (`normalize_entity_name`) + `metadata_store.get_merged_node_groups()` (L715) | `(name, entity_type)` 수렴, 크로스-문서 동일 엔티티 통합 |
| 그래프 API | `web/api/graph.py` (`/api/graph/full` L74, `/explore` L115, `/node/{id}` L187, `/merges` L247) | vis-network 페이로드, 노드 출처 문서, 병합 로그 노출 |
| MCP 그래프 도구 | `mcp/tools.py` `get_graph_context()` (L158) + `search_context()` | `entity_name` + `depth` 로 관계 서브그래프 반환 |
| 변경 감지/재처리 | `processor/reprocessor.py` (`check_and_mark_changed`, `delete_derived_data`, Delete&Recreate) + `web/api/git_sync.py` | `content_hash` 기반 변경 감지, 파생 데이터 재생성 |
| 어휘 단일 출처 | `processor/graph_vocabulary.py` (`ENTITY_TYPES`, `RELATION_TYPES`, `INTENT_TO_RELATIONS`) | 설계 용어 사전의 스키마 기반 |

### 그래프 성숙도에 대한 솔직한 평가 (현실성 기준선)

기존 하네스(graph-search-diagnosis, indexing-improvement, graph-overview-merge-quality)가 반복적으로 다뤄온 사실:

- **git_code AST 그래프 = 高신뢰**: `imports`/`contains`/심볼 FQN 은 AST 정적 추출(D-036)이라 100% 결정론적. 설계 자동화의 **가장 단단한 기반**.
- **Confluence 링크 그래프 = 中신뢰**: `references`/`mentions_*` 는 HTML 링크 기반 결정론적이지만, 페이지가 인덱싱돼 있어야만 노드가 산다(미인덱싱 페이지는 `page:{id}` placeholder).
- **문서 LLM 의미 그래프 = 低~中신뢰**: `depends_on`/`implements` 등 의미 관계는 LLM 추출이라 recall/precision 이 들쭉날쭉. graph-search-diagnosis 가 `graph_recall<10%` 를 다룬 이력이 있음. 의미 관계에 의존하는 시나리오는 **사람 검수 게이트 필수**.
- **엔티티 머지 = 中신뢰**: `normalize_entity_name` 은 룰 기반(deterministic)이나 동의어/약어 머지는 미흡(merge-quality 하네스의 진단 대상). 동일 개념의 노드 분산이 남아 있을 수 있음.

각 시나리오의 ⑥ 난이도와 별도로 **그래프품질 의존도**를 명시하여, 어느 시나리오가 현재 성숙도에서 바로 신뢰 가능한지 구분한다.

---

## 시나리오 1. 의존성 분석 & 레이어 위반 탐지

**기대효과:** import 그래프로 모듈 의존 맵·순환 의존·레이어 역방향 위반을 코드 기준 1회 스캔으로 자동 검출.

- **① 자동화 대상:** 모듈/패키지 간 의존 맵, 순환 의존(cycle), 아키텍처 레이어 위반(예: `storage → web` 역방향), 외부 라이브러리 인벤토리.
- **② 활용 building block:**
  - `ast_code_extractor.to_graph_data()` 의 `imports` 관계 (`module` 노드 간 엣지).
  - `graph_store.graph` (NetworkX DiGraph) — `nx.simple_cycles()`, `nx.descendants()` 등 그래프 알고리즘 직접 적용 가능.
  - `/api/graph/full` 로 전역 import 서브그래프 추출, 또는 `get_neighbors(module, depth)` 로 모듈별 의존.
  - 외부 의존 인벤토리: `imports` target 중 내부 `module` 노드로 수렴되지 않은 노드 = 외부 라이브러리.
- **③ gap/추가 필요분:**
  - **레이어 규칙 정의 파일 부재** — `config.yaml` 에 레이어 순서/허용 의존 규칙(예: `web > processor > storage`)을 선언하는 스키마가 없음. 신규 규칙 파일 + 위반 판정기 필요.
  - 패키지 단위 집계 부재(현재 노드는 파일 단위 `module`). 파일→패키지 롤업 로직 추가 필요.
  - 외부/내부 모듈 구분 휴리스틱 필요(현재 둘 다 `module` 타입).
- **④ 워크플로우 스케치:**
  ```
  git_code 인덱싱 → graph_store import 서브그래프 추출
    → 파일→패키지 롤업 → NetworkX cycle/위반 탐지
    → config 레이어 규칙 대조 → 위반 리스트 + 의존 매트릭스 산출
  ```
- **⑤ 산출물:** 의존 매트릭스(표), 순환 의존 목록, 레이어 위반 리포트(위반 엣지 + 출처 파일), 외부 의존 인벤토리 표. mermaid 의존 다이어그램.
- **⑥ 난이도:** **즉시 PoC** (import 그래프가 이미 정확, NetworkX 알고리즘만 얹으면 됨)
- **그래프품질 의존도:** **낮음** (AST `imports` 결정론적, D-036)

---

## 시나리오 2. 영향도 분석 (Impact Analysis)

**기대효과:** "이 함수/모듈을 바꾸면 무엇이 깨지나"를 PR/변경 단위로 그래프 탐색해 영향 반경을 자동 산출.

- **① 자동화 대상:** 특정 심볼/모듈 변경 시 영향받는 다운스트림(역방향 import + caller) 집합, 변경 영향 반경(blast radius), PR 단위 영향 요약.
- **② 활용 building block:**
  - `get_graph_context(entity_name, depth)` (MCP) / `get_connected_component(entity, depth)` — hop 거리별 영향 노드.
  - `imports` 관계의 **역방향 탐색**: `graph_store.graph.predecessors()` 로 "나를 import 하는 모듈".
  - `contains` 로 클래스↔메서드 포함 관계 추적.
  - `mcp/tools.py` 를 통해 LLM 에이전트(Claude Code)가 직접 질의 가능.
- **③ gap/추가 필요분:**
  - **호출 그래프(call graph) 부재** — AST 추출은 `imports`/`contains` 까지만. 실제 함수 호출 엣지(`calls`)는 LLM 의미 그래프에만 있고 코드 기준이 아님. import 도달성은 과대추정(파일 import ≠ 심볼 사용). 정밀 영향도엔 호출 그래프 추출 추가 필요.
  - PR diff → 변경된 심볼 매핑 로직 필요(git diff 라인 → `line_start/line_end` 매칭).
- **④ 워크플로우 스케치:**
  ```
  PR diff → 변경 심볼 FQN 식별(line 범위 매칭)
    → 각 심볼에 대해 predecessors BFS(depth N) → 영향 모듈/심볼 집합
    → 출처 문서/테스트 매핑 → "영향 반경" 요약
  ```
- **⑤ 산출물:** 영향 노드 트리(hop 별), 영향받는 파일/테스트 체크리스트(표), PR 코멘트용 요약 텍스트, mermaid 영향 서브그래프.
- **⑥ 난이도:** **중간** (import 도달성 PoC 는 쉬우나, 정밀 호출 그래프는 대형)
- **그래프품질 의존도:** **낮음~중간** (import 기반은 낮음, 정밀 call 기반으로 가면 중간)

---

## 시나리오 3. 아키텍처 다이어그램 자동 생성

**기대효과:** 코드/문서 그래프를 컴포넌트·의존·엔티티 관계 다이어그램으로 자동 렌더하여 항상 최신 상태의 그림 유지.

- **① 자동화 대상:** 컴포넌트 다이어그램, 모듈 의존 그래프, 엔티티 관계도(ERD 유사) 자동 생성 및 갱신.
- **② 활용 building block:**
  - `/api/graph/full` (vis-network JSON) — `/graph` 페이지에서 이미 인터랙티브 렌더(`web/api/graph.py` L74).
  - `_node_payload`/`_edge_payload` 가 `group=entity_type`, `label=relation_type` 부여 → 타입별 색상/그룹 분리.
  - `get_schema_summary()` (graph_store L637) — entity_type 별 대표 노드 요약으로 상위 추상 다이어그램 생성.
  - `entity_type` 필터로 뷰 분리(module만=의존도, system/team만=조직 컨텍스트).
- **③ gap/추가 필요분:**
  - **mermaid/PlantUML export 부재** — 현재 vis.js 인터랙티브뿐, 문서 임베드용 정적 텍스트 다이어그램 export 엔드포인트 없음. `/api/graph/export?format=mermaid` 추가 필요.
  - 추상화 레벨 컨트롤(파일→패키지→레이어 롤업) 부재. 300노드 상한(`_FULL_GRAPH_MAX_NODES`) 초과 시 degree 컷이라 의미 단위 그룹핑 아님.
- **④ 워크플로우 스케치:**
  ```
  graph_store → entity_type/관계 필터 + 추상화 롤업
    → vis.js(탐색용) | mermaid 텍스트(문서 임베드용) 동시 export
    → Confluence/ADR 문서에 자동 삽입
  ```
- **⑤ 산출물:** vis.js 인터랙티브 그래프(기존), mermaid `graph TD` 텍스트, 컴포넌트 다이어그램(타입별 그룹).
- **⑥ 난이도:** **중간** (인터랙티브는 즉시 PoC, mermaid export + 롤업이 중간)
- **그래프품질 의존도:** **중간** (의존 다이어그램은 낮음, 의미/조직 다이어그램은 LLM 그래프라 중간)

---

## 시나리오 4. 아키텍처 문서 자동 업데이트 (Doc Drift Auto-Patch)

**기대효과:** 코드 변경 시 영향받은 모듈 섹션만 그래프 diff 로 식별해 설계 문서를 부분 갱신 — 문서 표류(drift) 최소화.

- **① 자동화 대상:** git_code 재인덱싱 후 변경된 모듈/심볼/의존을 식별하여, 해당 설계 문서 섹션만 선별 갱신 제안.
- **② 활용 building block:**
  - `reprocessor.check_and_mark_changed()` + `content_hash` 비교 — 변경 문서 감지(`processor/reprocessor.py`).
  - `git_sync.py` (`/api/git/sync`) — git clone/pull + content_hash 기반 재인덱싱.
  - **그래프 diff**: 재인덱싱 전후 `graph_store` 스냅샷 비교(추가/삭제된 노드·엣지) → 변경된 모듈 집합.
  - `link_graph_builder` 의 `documents`/`references` 로 "어느 설계 문서가 이 모듈을 다루는가" 역추적.
  - LLM(`processor/llm_client.py`) + `search_context` 로 해당 섹션 재작성.
- **③ gap/추가 필요분:**
  - **그래프 스냅샷/diff 인프라 부재** — 현재 graph_store 는 현재 상태만. 인덱싱 전후 스냅샷 저장 + diff 계산기 신규 필요.
  - **코드 모듈 ↔ 설계 문서 섹션 매핑 부재** — 어떤 문서의 어떤 섹션이 어떤 모듈을 서술하는지 명시적 링크가 없음(현재는 의미 검색에 의존). 매핑 메타데이터 또는 섹션 앵커 필요.
  - 문서 자동 수정의 신뢰성 — LLM 재작성은 검수 게이트 필수.
- **④ 워크플로우 스케치:**
  ```
  git pull → content_hash 변경 감지 → 재인덱싱(Delete&Recreate)
    → 그래프 before/after diff → 변경 모듈 집합
    → 모듈→문서섹션 역매핑 → LLM 섹션 재작성(diff 컨텍스트 주입)
    → "변경 제안" PR/리뷰 큐 (자동 머지 X)
  ```
- **⑤ 산출물:** 변경 모듈 리스트, 영향 문서 섹션 목록, 섹션별 재작성 diff 제안(검수 대기), 변경 요약 리포트.
- **⑥ 난이도:** **대형** (그래프 diff + 섹션 매핑 + LLM 재작성 검수 파이프라인)
- **그래프품질 의존도:** **중간** (모듈 식별은 AST라 낮음, 문서 섹션 매핑/재작성은 의미 그래프+LLM라 중간)

---

## 시나리오 5. 설계 일관성/표류(Drift) 검증 — 문서 그래프 ↔ 코드 그래프 교차

**기대효과:** 설계 문서에 기술된 의도된 구조와 실제 코드 그래프를 대조해 불일치(미구현/문서화 누락/의존 어긋남)를 리포트.

- **① 자동화 대상:** "설계 의도(문서) vs 실제(코드)" 불일치 — 문서엔 있으나 코드에 없는 컴포넌트/의존, 코드엔 있으나 문서화 안 된 의존.
- **② 활용 building block:**
  - **코드 그래프**: `ast_code_extractor` 의 `module`/`imports`/`contains`.
  - **문서 그래프**: `llm_body_extractor` 의 `depends_on`/`provides`/`has_part` + `link_graph_builder` 의 `references`.
  - `entity_normalizer.normalize_entity_name()` — 두 그래프의 엔티티 이름 정규화 후 매칭 키 통일.
  - `get_merged_node_groups()` — 동일 엔티티가 코드/문서 양쪽에서 등장하는지(크로스-소스 수렴) 확인.
  - `graph_store.get_neighbors()` 로 양 소스의 엔티티별 이웃 집합 비교.
- **③ gap/추가 필요분:**
  - **엔티티 alignment 의 본질적 난이도** — 문서의 "결제 서비스"와 코드의 `payment_service.py` 를 동일시하려면 동의어/추상화 레벨 매핑 필요. `normalize_entity_name` 은 표기 정규화만, **의미 머지는 미흡**(merge-quality 하네스 진단 대상).
  - 불일치 vs 추상화 차이 구분 — 문서는 "Auth Service"(시스템), 코드는 파일 다수. 단순 set diff 는 false positive 폭증.
- **④ 워크플로우 스케치:**
  ```
  코드 그래프 + 문서 그래프 각각 추출
    → normalize_entity_name 으로 매칭 키 통일 → 엔티티 alignment
    → 의존 엣지 집합 비교(문서-only / 코드-only / 공통)
    → 사람 검수 게이트 → drift 리포트
  ```
- **⑤ 산출물:** 일치/불일치 매트릭스(표), "문서엔 있으나 코드 없음" 목록, "코드엔 있으나 미문서화" 목록, 신뢰도 라벨 부착(검수 필요).
- **⑥ 난이도:** **대형** (크로스-소스 엔티티 alignment 가 핵심 난제)
- **그래프품질 의존도:** **높음** (문서 LLM 의미 그래프 + 엔티티 머지 양쪽에 의존 — 현 성숙도에서 가장 취약. 검수 보조 도구로만 신뢰)

---

## 시나리오 6. 신규 기능 설계 컨텍스트 자동 조립

**기대효과:** 새 기능 기획 시 관련 기존 모듈·문서·결정사항을 그래프+RAG 로 자동 수집해 설계 시작점(브리프)을 제공.

- **① 자동화 대상:** 기능 키워드 입력 → 관련 코드 모듈, 관련 설계 문서, 관련 ADR/결정, 영향 가능 영역을 한 번에 묶은 "설계 브리프" 생성.
- **② 활용 building block:**
  - `search_context(query, include_graph=True)` (`mcp/tools.py` L20) — 벡터 + 그래프 결합 컨텍스트 조립(`context_assembler.py`).
  - `get_connected_component(keyword)` / `/api/graph/explore?keyword=` — 키워드 연결 엔티티 전체 서브그래프(임베딩 fallback 시드 포함, L131).
  - `graph_store.get_query_relevant_schema()` (L715) + `INTENT_TO_RELATIONS` (graph_vocabulary) — 질의 의도에 맞는 관계 타입 선별.
  - `list_documents` + `get_document` 로 관련 원본 회수.
- **③ gap/추가 필요분:**
  - **ADR/결정 인덱싱 부재** — `.context/decisions/DECISIONS.md` 가 문서 소스로 인덱싱돼 있지 않음(D-번호 단위 청킹/링크 필요). 시나리오 8과 연계.
  - 브리프 템플릿(관련 모듈/문서/결정/리스크 섹션) 표준화 필요.
  - "유사 과거 기능" 검색은 벡터 의미검색에 의존 — 정밀도 한계.
- **④ 워크플로우 스케치:**
  ```
  기능 키워드/요구사항 입력
    → search_context(그래프 포함) + explore(연결 엔티티)
    → 관련 모듈/문서/결정 회수 → 영향 영역(시나리오 2 영향도) 부착
    → LLM 으로 설계 브리프 조립
  ```
- **⑤ 산출물:** 설계 브리프 문서(관련 모듈 표 + 관련 문서 링크 + 관련 결정 + 초기 영향 영역 + 미해결 질문).
- **⑥ 난이도:** **중간** (search_context 가 이미 존재, 브리프 조립 + ADR 인덱싱이 추가분)
- **그래프품질 의존도:** **중간** (그래프는 회수 보강용이라 일부 누락 허용. 핵심은 벡터 검색이 떠받침)

---

## 시나리오 7. 도메인 모델/용어 사전 자동 구축

**기대효과:** 정규화·머지된 엔티티와 어휘 스키마로 사내 도메인 용어/엔티티 통합 사전을 자동 생성·유지.

- **① 자동화 대상:** 사내 엔티티(시스템/모듈/팀/개념/API) 통합 사전 — 표준 명칭, 동의어(병합된 표기), 출처 문서, 타입.
- **② 활용 building block:**
  - `get_merged_node_groups(min_variants=2)` (`metadata_store` L715) — 크로스-문서 수렴된 엔티티 그룹 = 동의어/이표기 사전의 핵심.
  - `/api/graph/node/{id}` (L187) — 노드별 출처 문서 + 병합 로그(`graph_merge_log`).
  - `graph_vocabulary.ENTITY_TYPES`/`RELATION_TYPES` — 타입별 분류 스키마.
  - `entity_normalizer.normalize_entity_name()` — 표기 정규화 키.
  - `/api/graph/merges` (L247) — 병합 그룹 목록(merge-quality 하네스 산출물).
- **③ gap/추가 필요분:**
  - **정의(definition) 자동 생성** — 엔티티 "이름"은 있으나 "정의"는 `description`(있으면)에만. 출처 문서에서 정의 문장 추출/요약 필요.
  - 동의어 머지 정밀도 — 약어("MSA"↔"Microservice Architecture") 같은 의미 동의어는 룰 기반 정규화로 안 잡힘.
  - 사전 export 포맷(용어집 마크다운/표) 부재.
- **④ 워크플로우 스케치:**
  ```
  graph_store 전체 노드 → get_merged_node_groups(동의어 클러스터)
    → 타입별 분류(ENTITY_TYPES) → 노드별 출처/정의 회수(/node/{id})
    → LLM 으로 정의 문장 요약 → 용어집 문서 생성
  ```
- **⑤ 산출물:** 도메인 용어집(표: 표준명 | 동의어 | 타입 | 정의 | 출처 문서 수), 타입별 엔티티 인벤토리.
- **⑥ 난이도:** **중간** (머지 그룹/출처 API 가 이미 존재, 정의 요약 + export 가 추가분)
- **그래프품질 의존도:** **중간** (머지 품질에 직접 의존 — merge-quality 하네스가 다뤄온 영역. git_code 엔티티는 정확, 문서 엔티티는 검수 권장)

---

## 시나리오 8. ADR(Architecture Decision Record) 보조 & 결정 영향 추적

**기대효과:** `.context/decisions` 패턴과 그래프를 연계해 변경 시 영향받는 과거 결정을 추적하고 ADR 작성을 보조.

- **① 자동화 대상:** ADR 작성 시 관련 기존 결정/모듈 자동 제시, 코드/설계 변경 시 영향받는 D-번호 결정 추적, supersedes 체인 관리.
- **② 활용 building block:**
  - `.context/decisions/DECISIONS.md` (D-001~D-042+) 패턴 — 결정을 문서 소스로 인덱싱.
  - `relation_type="supersedes"` (graph_vocabulary L91) — 결정 간 대체/폐기 관계를 그래프로 표현.
  - `search_context` / `get_graph_context` — 변경 키워드 → 관련 결정 회수.
  - 시나리오 4(그래프 diff)와 연계: 변경 모듈 → 그 모듈을 언급한 결정 역추적.
  - `INTENT_TO_RELATIONS` 의 "폐기/대체/마이그레이션 → supersedes" 매핑.
- **③ gap/추가 필요분:**
  - **결정↔코드 모듈 명시적 링크 부재** — D-036 이 `ast_code_extractor.py` 를 다룬다는 사실이 그래프 엣지로 없음. 결정 본문의 파일/모듈 언급을 `documented_in`/`mentions` 엣지로 추출 필요.
  - ADR 인덱싱 파이프라인(D-번호 단위 청킹 + 메타데이터) 신규.
  - supersedes 체인 시각화 부재.
- **④ 워크플로우 스케치:**
  ```
  DECISIONS.md 인덱싱(D-번호 단위) → 결정→모듈 mentions 엣지 추출
    → 코드/설계 변경 감지(시나리오 4) → 영향 모듈 → 관련 D-번호 역추적
    → "이 변경이 D-036 가정을 깨는가?" 경고 + 새 ADR 초안 보조
  ```
- **⑤ 산출물:** 영향받는 결정 목록(D-번호 + 관련도), supersedes 체인 다이어그램, ADR 초안(관련 결정/모듈 prefilled).
- **⑥ 난이도:** **대형** (ADR 인덱싱 + 결정↔모듈 엣지 추출 + 변경 추적 통합)
- **그래프품질 의존도:** **중간** (결정→모듈 mentions 엣지 추출이 LLM/룰 혼합. 회수 보조라 일부 누락 허용)

---

## 시나리오 9. (창의적) 온보딩용 "코드 투어" 자동 생성

**기대효과:** 신규 개발자에게 진입점→핵심 모듈→의존 순서로 정렬된 학습 경로를 그래프 중심성 기반으로 자동 생성.

- **① 자동화 대상:** 신규 입사자/신규 컨트리뷰터를 위한 모듈 학습 순서, 핵심 허브 모듈, 진입점(entrypoint) 식별, 각 모듈 1줄 요약.
- **② 활용 building block:**
  - `graph_store.graph` (NetworkX) — `nx.degree_centrality()`, `nx.pagerank()` 로 핵심 허브 모듈 랭킹.
  - `imports` 그래프의 위상 정렬(topological order) — 의존 낮은 기반 모듈 → 상위 모듈 학습 순서.
  - `get_neighbors(module)` — 모듈별 직접 의존 컨텍스트.
  - `search_context` / `get_document` — 모듈별 관련 설계 문서/요약 회수.
  - `/api/graph/explore` — 모듈 중심 서브그래프 시각화로 투어 시각 자료.
- **③ gap/추가 필요분:**
  - **entrypoint 휴리스틱** — `main`/`create_app`/CLI 엔트리 식별 룰 필요(현재 노드 타입엔 entrypoint 개념 없음).
  - "학습 순서" 정렬 알고리즘(중심성 + 위상 + 문서 존재 여부 가중) 설계 필요.
  - 모듈 요약은 `description`(시그니처)뿐 — 책임 요약은 LLM 필요.
- **④ 워크플로우 스케치:**
  ```
  import 그래프 → pagerank(허브) + topological sort(기반→상위)
    → entrypoint 식별 → 모듈별 문서/요약 회수
    → 학습 경로(순서 + 1줄 요약 + 서브그래프 그림) 생성
  ```
- **⑤ 산출물:** 코드 투어 문서(순서 매겨진 모듈 리스트 + 1줄 책임 + 의존 관계 + 진입점 표시), 모듈별 서브그래프 이미지.
- **⑥ 난이도:** **중간** (중심성/위상은 즉시 PoC, 학습순서 가중·요약이 추가분)
- **그래프품질 의존도:** **낮음** (import 그래프 중심성 기반 — AST라 정확. 요약만 LLM)

---

## 시나리오 10. (창의적) 설계 안티패턴/리스크 휴리스틱 스캐너

**기대효과:** 그래프 구조 지표로 갓 클래스·순환·과결합·고아 모듈 등 설계 리스크를 정량 스코어링하여 리팩터링 우선순위 제시.

- **① 자동화 대상:** 구조적 설계 냄새(smell) 탐지 — 갓 모듈(과도한 fan-in/out), 순환 의존, 고아 모듈(연결 0), 단일 책임 위반(거대 클래스 contains 과다), 불안정 의존(상위가 하위에 강결합).
- **② 활용 building block:**
  - `graph_store.graph` — `in_degree`/`out_degree`(fan-in/out), `nx.simple_cycles`(순환), 약연결 컴포넌트(고아).
  - `contains` 관계 카운트 — 클래스당 메서드 수(거대 클래스 후보).
  - `stats()` (L628) / `get_schema_summary()` (L637) — 전역 분포 기준선.
  - Martin 안정성 지표(I = Ce/(Ca+Ce)) 를 `imports` in/out 으로 계산 가능.
  - `/api/graph/full` 로 핫스팟 시각 강조(degree 컷이 이미 허브 우선).
- **③ gap/추가 필요분:**
  - **임계값 정책** — "fan-out 몇 이상이 god module인가" 등 임계값을 코드베이스 분포 기반으로 자동 산정 또는 config 화 필요.
  - 스코어 합성/랭킹 로직 신규.
  - 안티패턴 카탈로그(룰셋) 정의.
- **④ 워크플로우 스케치:**
  ```
  import/contains 그래프 → 모듈별 fan-in/out, 순환 참여, contains 수,
    안정성 지표 계산 → 분포 기반 임계 → 리스크 스코어 합성
    → 우선순위 랭킹 + 시각 핫스팟
  ```
- **⑤ 산출물:** 리스크 랭킹 표(모듈 | fan-in | fan-out | 순환참여 | 안정성 | 스코어), 핫스팟 강조 그래프, 리팩터링 우선순위 목록.
- **⑥ 난이도:** **중간** (그래프 지표는 즉시 PoC, 임계 정책·스코어 합성이 추가분)
- **그래프품질 의존도:** **낮음** (import/contains 구조 지표 기반 — AST라 정확. 의미 해석 불필요)

---

## 11. 요약 표

| # | 시나리오 | 핵심 building block | 난이도 | 그래프품질 의존도 | 기대효과 |
|---|----------|--------------------|--------|-----------------|---------|
| 1 | 의존성 분석 & 레이어 위반 | `ast_code_extractor.imports` + NetworkX cycles + `/api/graph/full` | 즉시 PoC | **낮음** | import 그래프로 의존맵·순환·레이어 위반 1회 스캔 검출 |
| 2 | 영향도 분석 | `get_graph_context(depth)` + `graph.predecessors()` + MCP | 중간 | 낮음~중간 | 변경 blast radius 를 PR 단위로 자동 산출 |
| 3 | 아키텍처 다이어그램 자동 생성 | `/api/graph/full`(vis.js) + `get_schema_summary` + mermaid export | 중간 | 중간 | 항상 최신인 컴포넌트/의존/관계 다이어그램 |
| 4 | 아키텍처 문서 자동 업데이트 | `reprocessor`+`content_hash` + 그래프 diff + `search_context` | 대형 | 중간 | 코드 변경 시 영향 섹션만 부분 갱신 제안 |
| 5 | 설계 일관성/표류 검증 | 코드그래프 ↔ 문서그래프 + `normalize_entity_name` + `get_merged_node_groups` | 대형 | **높음** | 의도(문서) vs 실제(코드) 불일치 리포트 |
| 6 | 신규 기능 설계 컨텍스트 조립 | `search_context(include_graph)` + `/api/graph/explore` + `INTENT_TO_RELATIONS` | 중간 | 중간 | 관련 모듈/문서/결정 묶은 설계 브리프 자동 생성 |
| 7 | 도메인 모델/용어 사전 | `get_merged_node_groups` + `/api/graph/node/{id}` + `graph_vocabulary` | 중간 | 중간 | 동의어·출처 통합 도메인 용어집 자동 구축 |
| 8 | ADR 보조 & 결정 영향 추적 | `.context/decisions` 인덱싱 + `supersedes` + `get_graph_context` | 대형 | 중간 | 변경 시 영향받는 과거 결정 추적 + ADR 보조 |
| 9 | 온보딩 코드 투어 자동 생성 | NetworkX pagerank/topo-sort(`imports`) + `get_neighbors` | 중간 | **낮음** | 진입점→핵심→의존 순 학습 경로 자동 생성 |
| 10 | 설계 안티패턴/리스크 스캐너 | `graph` fan-in/out + `simple_cycles` + `contains` 카운트 + 안정성 지표 | 중간 | **낮음** | 구조 리스크 정량 스코어링 + 리팩터링 우선순위 |

### 권고 우선순위 (그래프 성숙도 현실 반영)

- **지금 바로 신뢰 가능 (낮은 의존도, git_code AST 기반):** 시나리오 1 → 9 → 10 → 2. 모두 `imports`/`contains` 결정론 그래프 위에서 동작하므로 현 성숙도에서 안전.
- **보강 후 도입 (중간 의존도):** 시나리오 3, 6, 7, 8 — 의미 그래프/머지에 부분 의존하나 검수 보조 도구로 가치.
- **장기/검수 필수 (높은 의존도):** 시나리오 5 — 문서 LLM 의미 그래프 + 엔티티 alignment 가 핵심이라 현 성숙도에선 false positive 위험. merge-quality / indexing-improvement 하네스로 그래프 품질을 먼저 끌어올린 뒤 신뢰 가능.
