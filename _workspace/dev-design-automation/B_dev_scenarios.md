# 개발 업무 자동화 시나리오 설계

> 대상 시스템: **project-context-loop-system** (origin/main 기준, `/tmp/clp-origin-main`)
> 작성 관점: 시스템의 기존 역량(인덱싱 + 그래프 + MCP 서버 + REST/RAG + 평가 하네스)을 **개발 업무 자동화**에 어떻게 재사용할지 시나리오 설계. 구현이 아닌 분석·설계.

---

## 0. 시스템 핵심 역량 요약 (building block 인벤토리)

시나리오를 도출하기 전, 코드에서 확인한 재사용 가능한 역량을 정리한다.

| 역량 | 구현 위치 | 핵심 사실 (코드 근거) |
|------|----------|----------------------|
| **MCP 서버 4 tools** | `src/context_loop/mcp/tools.py` | `search_context(query, max_chunks, include_graph, include_source_code)`, `list_documents`, `get_document(format=original/chunks/graph)`, `get_graph_context(entity_name, depth)`. stdio/SSE 전송. → **Claude Code가 즉시 클라이언트로 붙을 수 있음** |
| **AST 코드 심볼/그래프 추출** | `processor/ast_code_extractor.py` | Python은 `ast`로 정확 추출(함수/메서드/클래스/모듈 docstring/모듈 상수), 데코레이터 포함 body, 정밀 import(`import_symbols` = `(module, symbol)`). Go/Java/TS/JS는 키워드+중괄호. 심볼은 FQN(`file.py::Class.method`). `to_graph_data`가 `imports` / `contains` 관계 생성 |
| **import / contains 그래프 + 양방향 탐색** | `storage/graph_store.py` | `get_neighbors`(양방향 BFS, 4단계 이름 폴백+임베딩 fallback), `get_connected_component`(연결 컴포넌트 전체, hop 거리 부여), `get_schema_summary`/`format_schema_for_llm`, 엔티티 임베딩 유사도 검색, 크로스-문서 정규 노드 병합 |
| **멀티뷰 인덱싱 파이프라인** | `processor/pipeline.py` | git_code = body+meta 뷰 임베딩; 문서 = body+meta+가상질문(question) 뷰. LLM 본문 그래프(의미관계 depends_on/implements/owned_by) opt-in. 결정론 처리(classifier 제거, 결과에서 storage_method 파생) |
| **RAG 컨텍스트 조립 + 출처** | `mcp/context_assembler.py` (`assemble_context`, `assemble_context_with_sources`), `web/api/chat.py` | 벡터+그래프 결합, reranker/HyDE/query expansion 옵션, 출처(sources) 메타데이터 반환, NDJSON 스트리밍 |
| **전역 그래프 API** | `web/api/graph.py` | `/api/graph/full`(degree 상위 추림), `/api/graph/explore?keyword`(서브그래프), `/api/graph/node/{id}`(출처 문서 + 병합 내역), `/api/graph/merges`(중복/병합 그룹) |
| **Git 코드 수집 + 증분 감지** | `ingestion/git_repository.py`, `ingestion/git_config.py` | `content_hash` 기반 created/updated/unchanged 분류, config의 repo/branch/product/category 스코프, 멀티에이전트 LLM 엔드포인트 |
| **문서 CRUD / 처리 트리거** | `web/api/documents.py` | `POST /api/documents`, `POST /api/documents/{id}/process`, 청크/그래프/메타데이터 탭, `processing_history` |
| **합성 골드셋 + 평가 하네스** | `scripts/build_synthetic_gold_set.py`, `eval/*`, `scripts/eval_search.py` | 역방향 질문 생성 + Judge 게이트, chunk/document/graph 채점, Recall/Precision/MRR/nDCG |

핵심 통찰 — **MCP 서버가 이미 있으므로 "Claude Code/사내 에이전트가 이 MCP에 붙어 사내지식을 질의"하는 워크플로우는 거의 0 추가비용**이다. 아래 시나리오의 난이도 라벨은 이 사실을 적극 반영한다.

난이도 라벨 정의:
- **🟢 지금 당장 PoC 가능**: 기존 tool/API 호출 + 프롬프트 조합만으로 동작. 신규 코드 거의 불필요.
- **🟡 중간 작업 필요**: 기존 building block은 충분하나, 얇은 신규 어댑터(스크립트/엔드포인트 1~2개)나 데이터 정리가 필요.
- **🔴 큰 신규 개발 필요**: 핵심 데이터/추출 능력 자체가 부족해 새 모듈을 만들어야 함.

---

## 시나리오 1. 코드 리뷰 자동화 (PR diff → 컨벤션·과거결정·유사코드 기반 리뷰 코멘트)

**난이도: 🟡 중간 작업 필요**

### ① 무엇을 자동화하나
PR diff를 입력받아 (a) 사내 코딩 컨벤션 위반, (b) 과거 설계 결정(DECISIONS/문서)과의 정합성, (c) 유사 기존 구현과의 중복/일관성, (d) 변경 함수의 영향 범위를 자동 분석하여 리뷰 코멘트 초안을 생성한다.

### ② building block
- **`search_context`** (`mcp/tools.py`): diff에 등장하는 식별자·도메인 용어로 사내 컨벤션 문서·유사 코드 청크를 끌어온다. git_code 청크는 `include_source_code=True`로 원본 코드까지 첨부.
- **가상질문 + meta 뷰 인덱싱** (`pipeline.py`): "이 패턴을 어떻게 처리하나" 같은 자연어 질의가 잘 매칭됨.
- **`get_graph_context(entity_name, depth)` + `get_neighbors` 양방향 BFS** (`graph_store.py`): 변경된 함수 FQN을 시드로 **누가 이 함수를 import/contains 하는가**(predecessor)를 따라 영향 함수/파일을 식별. `_resolve_seed_nodes`의 짧은 이름 폴백 덕에 `create_user`만 줘도 매칭.
- **DECISIONS**: `.context/decisions/`를 문서로 인덱싱하면 과거 결정이 청크로 검색됨.

### ③ gap / 추가 필요
- **diff 파서 + 심볼 매핑**: diff hunk → 변경된 심볼 FQN으로 매핑하는 어댑터가 없음. `ast_code_extractor`는 파일 전체를 받으므로, "변경된 라인이 어느 심볼에 속하는지"를 line_start/line_end로 매핑하는 얇은 로직 필요.
- **PR 플랫폼 연동**: GitHub/GitLab 웹훅 → diff 수집 → 코멘트 게시 글루. (Claude Code가 `gh` CLI로 대신할 수 있어 PoC는 우회 가능)
- **영향 그래프의 한계**: 현재 그래프는 `imports`/`contains`만 — **함수 호출(call) 관계는 없음**. "이 함수를 호출하는 곳" 정밀 추적은 import 단위까지만 가능.

### ④ 워크플로우 스케치
```
PR diff 입력
  → 변경 파일/심볼 추출 (ast_code_extractor + line 매핑)
  → 심볼별로 search_context(식별자+컨벤션 키워드) → 유사코드·컨벤션·결정 청크
  → get_graph_context(변경 심볼 FQN) → import/contains 영향 범위
  → LLM이 (diff + 검색 컨텍스트 + 영향 그래프)로 리뷰 코멘트 합성
  → 코멘트 초안 (파일:라인 + 근거 출처 링크)
```

### ⑤ 사용자가 보는 결과물
PR에 달릴 리뷰 코멘트 초안 목록 — 각 코멘트에 "왜"(인용된 컨벤션 문서/과거 결정/유사 구현 출처)와 "영향받는 함수 목록"이 첨부됨.

**기대효과 1줄:** 리뷰어가 0에서 시작하지 않고, 사내 맥락이 반영된 근거 있는 코멘트 초안에서 출발한다.

---

## 시나리오 2. 설계/기술 문서 자동 생성 (모듈 설계서·API 레퍼런스·README 초안)

**난이도: 🟡 중간 작업 필요**

### ① 무엇을 자동화하나
인덱싱된 코드베이스에서 모듈별 설계 문서, API 레퍼런스, README 초안을 생성한다. 그래프로 모듈 구조를, 청크로 시그니처/docstring을 끌어온다.

### ② building block
- **`/api/graph/explore?keyword=<module>` & `get_connected_component`** (`web/api/graph.py`, `graph_store.py`): 모듈을 시드로 연결된 심볼·import를 hop 거리와 함께 추출 → 구조도.
- **`get_document(format=graph)` / `format=chunks`** (`mcp/tools.py`): 문서(=파일/카테고리)의 노드·엣지와 심볼 청크 일괄 회수.
- **AST가 보존한 풍부한 시그니처** (`ast_code_extractor._python_func_sig`, `_python_class_sig`): `*args/**kwargs`, 반환타입, base class까지 보존 → API 레퍼런스 정확도.
- **모듈 docstring 심볼**(`__module__`): "이 모듈이 무엇인가"의 1차 시그널이 이미 인덱싱됨.
- **`format_schema_for_llm`**: 그래프 구조를 LLM 프롬프트용 텍스트로 바로 직렬화.

### ③ gap / 추가 필요
- **문서 생성 오케스트레이터**: "모듈 목록 → 모듈별 그래프+청크 수집 → LLM 작성 → md 파일 산출"을 도는 스크립트가 없음. 단, git_code 파이프라인의 멀티에이전트(worker/synthesizer) 패턴(`git_config.build_llm_client`)이 청사진으로 존재.
- **호출/데이터흐름 다이어그램**: import 그래프만으로는 시퀀스 다이어그램까지 못 그림(call 관계 부재).

### ④ 워크플로우 스케치
```
모듈/카테고리 목록 (list_documents source_type=git_code)
  → 각 모듈: get_document(graph) + get_document(chunks) + explore(module)
  → format_schema_for_llm 로 구조 직렬화
  → LLM: 모듈 설계서 + API 레퍼런스 섹션 작성
  → 합성기 LLM: 전체 README 초안으로 통합
  → markdown 산출
```

### ⑤ 사용자가 보는 결과물
`docs/generated/<module>.md` 형태의 모듈 설계서 + API 레퍼런스 + 루트 README 초안. 각 항목에 코드 출처(FQN) 표기.

**기대효과 1줄:** 신규/레거시 모듈의 문서 공백을, 코드에서 자동 파생된 정확한 초안으로 메운다.

---

## 시나리오 3. 기술 부채 분석 (순환 의존·god-module·TODO/FIXME·문서 공백)

**난이도: 🟡 중간 작업 필요**

### ① 무엇을 자동화하나
import 그래프로 순환 의존/허브(god-module)를, 코드 청크에서 TODO/FIXME를, 그래프-문서 매핑으로 "문서 없는 핵심 모듈"을 탐지하여 부채 리포트를 생성한다.

### ② building block
- **NetworkX 그래프 직접 접근** (`graph_store.graph`): `imports` 엣지로 구성된 DiGraph에 `nx.simple_cycles`, degree 분석을 바로 적용 가능. `/api/graph/full`이 이미 **degree 상위 노드**를 추려 반환(`_FULL_GRAPH_MAX_NODES`, degree sort) → 허브 식별 1차 신호.
- **`get_merged_node_groups` / `/api/graph/merges`**: 같은 엔티티가 여러 문서/파일에서 중복 정의된 그룹 노출 → 중복 추상화 후보.
- **코드 청크 전수**(`get_document(chunks)`, `meta_store`): 본문에 TODO/FIXME 정규식 스캔.
- **`get_schema_summary`**: 엔티티/관계 유형 분포로 구조 건강도 한눈에.

### ③ gap / 추가 필요
- **순환·중심성 분석 스크립트**: 그래프 객체는 있으나 `simple_cycles`/betweenness를 돌려 랭킹하는 분석 레이어가 없음(얇은 신규 스크립트).
- **커버리지 빈약 영역**: 테스트 커버리지 데이터는 시스템 밖. coverage.xml 등을 별도 인덱싱하거나 외부 입력 필요.
- **call 그래프 부재**: 순환 의존은 파일/모듈 import 수준까지만. 함수 수준 순환은 못 봄.

### ④ 워크플로우 스케치
```
graph_store.graph (DiGraph, imports 엣지)
  → nx.simple_cycles → 순환 의존 목록
  → in/out-degree 랭킹 → god-module 후보
  → /api/graph/merges → 중복 엔티티 그룹
  → 전 청크 스캔 → TODO/FIXME 집계
  → 그래프 module 노드 ↔ code_doc 문서 매핑 → 문서 공백 모듈
  → LLM: 우선순위화된 부채 리포트
```

### ⑤ 사용자가 보는 결과물
부채 리포트(우선순위표): 순환 의존 사이클 / 허브 모듈 Top-N / 중복 추상화 / TODO 밀집 파일 / 문서 없는 핵심 모듈.

**기대효과 1줄:** "어디부터 리팩터링할까"를 그래프 근거로 객관적으로 우선순위화한다.

---

## 시나리오 4. 온보딩 자료 자동 생성 (코드베이스/도메인 핵심 가이드)

**난이도: 🟢 지금 당장 PoC 가능**

### ① 무엇을 자동화하나
신규 입사자용 "이 코드베이스/도메인의 핵심" 가이드를, 그래프 허브 엔티티 + 핵심 문서 청크로 조립한다.

### ② building block
- **`search_context`** (MCP): "이 시스템의 아키텍처/핵심 모듈/주요 도메인 개념은?" 같은 온보딩 질문을 그대로 던지면 청크+그래프 컨텍스트가 출처와 함께 조립됨.
- **`/api/graph/full` degree 추림**: 허브 엔티티(가장 많이 연결된 핵심 개념/모듈)를 자동 부각.
- **`get_connected_component(keyword)`**: 핵심 키워드(예: 제품명) 주변 전체 서브그래프를 hop별로.
- **Confluence 본문 그래프 + 가상질문 인덱싱**: 도메인 문서가 자연어 질의에 잘 매칭.

### ③ gap / 추가 필요
- 거의 없음. **MCP 서버에 Claude Code를 붙이고 온보딩 질문 세트를 던지는 것만으로 PoC 성립.** 산출물을 md로 정리하는 프롬프트 템플릿 정도만 추가.
- (선택) 허브 엔티티 자동 선정 로직을 스크립트화하면 반복 생성 자동화.

### ④ 워크플로우 스케치
```
온보딩 질문 세트(아키텍처/핵심모듈/도메인용어/시작점)
  → search_context (각 질문) → 출처 포함 컨텍스트
  → /api/graph/full → 허브 엔티티 Top-N
  → LLM: "0일차 가이드" md 조립 (각 항목에 출처 링크)
```

### ⑤ 사용자가 보는 결과물
신규자용 온보딩 md: 핵심 모듈 지도 + 도메인 용어집 + "여기부터 읽어라" 문서 링크 + 핵심 엔티티 그래프 이미지(/graph 페이지 활용).

**기대효과 1줄:** 온보딩 문서를 사람이 쓰지 않아도, 항상 최신 코드/문서에서 자동 파생된 가이드를 받는다.

---

## 시나리오 5. 버그 분류·원인 위치 추정 (triage)

**난이도: 🟢 지금 당장 PoC 가능**

### ① 무엇을 자동화하나
에러 로그/스택트레이스/이슈 텍스트를 입력받아 관련 모듈·심볼·과거 문서를 제시하고 1차 원인 후보를 좁힌다.

### ② building block
- **`search_context`**: 스택트레이스의 함수명·메시지로 관련 코드 청크(원본 코드 포함)·과거 트러블슈팅 문서를 검색. 멀티뷰(meta) 임베딩이 식별자 친화적.
- **`get_graph_context` / `get_neighbors` 양방향**: 에러난 함수 FQN을 시드로 import/contains 이웃을 따라 "함께 의심할 모듈" 확장. sink 노드여도 predecessor를 따라가는 양방향 BFS가 funnel 손실을 줄임.
- **임베딩 fallback**(`_resolve_seed_nodes`): 로그의 표기와 인덱스 표기가 달라도 의미 유사로 시드 매칭.

### ③ gap / 추가 필요
- **스택트레이스 파서**: 프레임 → 파일:라인 → 심볼 FQN 매핑 어댑터. 단, Claude Code가 자연어로 처리하면 PoC는 우회 가능.
- **이슈 트래커 연동**: 자동 분류 라벨링까지 가려면 Jira/GitHub Issues 글루 필요(Confluence mention/Jira 키는 이미 그래프 엔티티로 추출됨 — 연결 고리 존재).

### ④ 워크플로우 스케치
```
에러 로그 / 이슈 텍스트
  → search_context(메시지+함수명) → 관련 코드·문서 청크
  → 스택 프레임 → 심볼 FQN → get_graph_context → 의심 모듈 확장
  → LLM: 원인 후보 랭킹 + "확인할 파일:라인" 목록
```

### ⑤ 사용자가 보는 결과물
triage 카드: 의심 모듈/심볼 Top-N(근거 출처) + 관련 과거 이슈/문서 + 다음 확인 지점.

**기대효과 1줄:** 처음 보는 버그라도 "어느 코드/문서를 먼저 볼지"를 즉시 좁혀준다.

---

## 시나리오 6. 변경 영향 테스트 식별 + 테스트 생성 보조

**난이도: 🟡 중간 작업 필요**

### ① 무엇을 자동화하나
(a) 변경된 심볼로부터 영향받는 모듈을 그래프로 추적해 "다시 돌려야 할 테스트"를 식별하고, (b) 대상 함수의 시그니처/docstring/유사 테스트를 끌어와 테스트 케이스 초안을 생성한다.

### ② building block
- **`contains`/`imports` 그래프 + 양방향 BFS**: 변경 심볼 → predecessor(나를 쓰는 모듈) → 그 모듈을 커버하는 테스트 파일(테스트도 git_code로 인덱싱되면 import로 연결됨).
- **정밀 import (`import_symbols`, label)**: `to_graph_data`가 `from x import a,b`의 심볼명을 `imports` 관계 label에 노출 → "이 심볼을 import하는 곳" 질의가 살아있음.
- **AST 시그니처 + docstring 청크**: 테스트 생성 시 인자/반환/예외 정보 제공.
- **`search_context`**: 유사 함수의 기존 테스트를 few-shot 예시로 검색.

### ③ gap / 추가 필요
- **테스트↔대상 매핑**: 현재 그래프엔 "이 테스트가 이 함수를 검증한다"는 명시 관계가 없음. import 기반 근사치만. 정밀하려면 테스트 파일의 import 심볼 → 대상 매핑 어댑터 필요.
- **call 관계 부재**가 영향 범위 정밀도를 제한(시나리오 1과 동일 한계).

### ④ 워크플로우 스케치
```
변경 심볼 FQN
  → get_neighbors(predecessor) → 영향 모듈
  → 영향 모듈을 import하는 test_* 문서 식별 → "재실행 대상 테스트"
  → 대상 함수: get_document(chunks)로 시그니처+body
  → search_context(유사 테스트) → few-shot
  → LLM: 테스트 케이스 초안
```

### ⑤ 사용자가 보는 결과물
"이번 변경으로 재실행 권장 테스트 목록" + 신규/누락 케이스 테스트 코드 초안.

**기대효과 1줄:** 전체 테스트를 맹목적으로 돌리는 대신, 영향 범위에 집중하고 빈 케이스를 메운다.

---

## 시나리오 7. 커밋/PR 메시지·체인지로그 자동화

**난이도: 🟡 중간 작업 필요**

### ① 무엇을 자동화나
스테이징된 diff와 변경 심볼의 사내 맥락(관련 결정/문서)을 결합해 Conventional Commits 메시지, PR 본문, 릴리스 체인지로그를 생성한다.

### ② building block
- **`content_hash` 기반 변경 감지**(`git_repository.py`): created/updated 파일 분류가 이미 있어 "무엇이 바뀌었나"의 1차 신호 제공.
- **변경 심볼 → `search_context`**: 변경 영역의 도메인 의미·관련 과거 결정을 끌어와 "왜"를 메시지에 반영.
- **`get_graph_context`**: 변경이 닿는 상위 모듈명을 scope(`feat(web): ...`)로 자동 추론.

### ③ gap / 추가 필요
- **diff → 심볼 매핑**(시나리오 1과 공유) + 커밋 메시지 컨벤션 프롬프트 템플릿.
- **PR/릴리스 글루**: `gh` CLI로 우회 가능하므로 PoC 진입장벽은 낮음.

### ④ 워크플로우 스케치
```
git diff (staged)
  → 변경 파일/심볼 → scope 추론(graph) + 의미 보강(search_context)
  → LLM: Conventional Commit / PR 본문 / 체인지로그 항목
```

### ⑤ 사용자가 보는 결과물
커밋 메시지 초안 + PR 설명(변경 요약·영향 모듈·관련 결정 링크) + 체인지로그 라인.

**기대효과 1줄:** "what"만이 아니라 사내 "why"가 담긴 메시지를 자동으로 얻는다.

---

## 시나리오 8. 평가 하네스 재활용 — 검색/문서 품질 회귀 게이트

**난이도: 🟢 지금 당장 PoC 가능**

### ① 무엇을 자동화하나
인덱싱·검색 품질을 CI에서 자동 측정해 "코드/문서를 추가했더니 검색 품질이 떨어졌다"를 회귀로 잡는다. 더 나아가 **위 자동화 시나리오들이 의존하는 RAG 품질의 가드레일** 역할.

### ② building block
- **`scripts/build_synthetic_gold_set.py`**: 인덱싱된 git_code/confluence에서 역방향 질문 + Judge 게이트로 골드셋 자동 합성. Generator/Judge 분리로 편향 완화.
- **`scripts/eval_search.py` + `eval/metrics.py`**: Recall/Precision/MRR/nDCG, chunk/document/graph 채점.
- **`eval/graph_match.py`**: 그래프 엔티티 매칭 평가 → 시나리오 1·3·6이 의존하는 그래프 품질도 측정 가능.

### ③ gap / 추가 필요
- **CI 글루 + 임계값 게이트**: "MRR < X면 실패" 같은 게이트 스크립트(얇음).
- 골드셋의 self-eval bias는 별도 감사 하네스(`rag-eval-audit`)가 이미 존재 — 신뢰성 관리 체계도 갖춰짐.

### ④ 워크플로우 스케치
```
인덱싱 갱신 후
  → build_synthetic_gold_set (또는 고정 골드셋 로드)
  → eval_search → 메트릭
  → 임계값 비교 → CI pass/fail + 메트릭 추세 리포트
```

### ⑤ 사용자가 보는 결과물
CI 체크: 검색 품질 메트릭 추세 + 회귀 경고. 자동화 시나리오 전반의 "신뢰 기반선".

**기대효과 1줄:** 지식 플랫폼 위의 모든 개발 자동화가 의존하는 검색 품질을 회귀로부터 보호한다.

---

## 시나리오 9. (창의) 아키텍처 표류 감지 — 설계 문서 ↔ 실제 코드 정합성 진단

**난이도: 🔴 큰 신규 개발 필요 (부분 🟡)**

### ① 무엇을 자동화하나
Confluence 설계 문서가 명시한 의도 관계(예: "A는 B에만 의존")와, AST로 추출한 실제 import 그래프를 비교해 **문서-코드 표류(drift)**를 탐지한다.

### ② building block
- **두 그래프 소스가 한 GraphStore에 공존**: Confluence는 LLM 본문 그래프(`depends_on/implements/owned_by` 의미관계, `llm_body_extractor`), git_code는 AST `imports`. **정규 노드 병합**(`save_graph_data`)으로 같은 엔티티명이면 수렴 → 같은 노드에서 "문서가 말한 관계"와 "코드의 실제 관계"를 동시에 볼 잠재력.
- **`/api/graph/node/{id}`**: 노드별 출처 문서(설계 문서 vs 코드)와 병합 내역 노출.

### ③ gap / 추가 필요
- **관계 출처 구분 + 의도 vs 실제 대조 로직**: 엔티티는 병합되지만 "이 엣지는 설계문서 발(의도)인가 코드 발(실제)인가"를 구분해 diff하는 분석기가 없음 → 신규 모듈.
- **엔티티명 정합**: 설계 문서의 "인증 서비스"와 코드의 `auth_service.py::AuthService`가 같은 노드로 병합될 보장이 약함(표기 차이). 별칭/임베딩 매칭 보강 필요.

### ④ 워크플로우 스케치
```
설계 문서 그래프(의도 관계) vs git_code 그래프(실제 import)
  → 공통 엔티티 정렬(정규 병합 + 임베딩)
  → 의도엔 있는데 코드엔 없는 관계 / 코드엔 있는데 문서엔 없는 관계 diff
  → LLM: 표류 항목 설명 + 문서 갱신 or 리팩터링 제안
```

### ⑤ 사용자가 보는 결과물
표류 리포트: "문서는 A→B만 말하지만 코드는 A→C도 의존" 같은 불일치 목록 + 제안.

**기대효과 1줄:** 설계 의도와 실제 구현의 괴리를 자동으로 드러내 문서/코드를 동기화한다.

---

## 시나리오 10. (창의) 코드/문서 검색 MCP를 IDE·에이전트에 상시 연결한 "사내 맥락 어시스트"

**난이도: 🟢 지금 당장 PoC 가능**

### ① 무엇을 자동화하나
개발자가 작업 중 "이 함수 누가 써?", "이 도메인 규칙 어디 문서화돼 있어?", "비슷한 구현 있어?"를 IDE/터미널의 Claude Code에서 그대로 묻고, 이 시스템 MCP가 코드+문서+그래프 통합 답을 출처와 함께 즉답한다.

### ② building block
- **MCP 서버 + 4 tools 전체**: `search_context`(통합 RAG), `get_graph_context`(관계), `get_document`(원본), `list_documents`. **stdio 전송 = subprocess로 즉시 연결**. claude.md에 클라이언트 설정 예시까지 명시됨.
- **출처 포함 조립**(`assemble_context_with_sources`): 답변에 문서 링크/FQN 첨부.
- **양방향 그래프 + 임베딩 fallback**: 짧은 이름/표기 차이에 강건.

### ③ gap / 추가 필요
- **거의 없음.** 인덱스가 채워져 있고 MCP가 떠 있으면 끝. 팀 배포용 SSE 모드/접근 설정 정도만.
- (선택) IDE별 클라이언트 설정 배포 자동화.

### ④ 워크플로우 스케치
```
IDE/터미널 Claude Code
  → MCP search_context / get_graph_context / get_document
  → 출처 포함 통합 답변 (코드+문서+관계)
```

### ⑤ 사용자가 보는 결과물
작업 흐름을 끊지 않는 인라인 사내 맥락 답변(출처 링크 포함). 시나리오 1·5·6의 공통 인터페이스이기도 함.

**기대효과 1줄:** 가장 낮은 비용으로 가장 큰 일상 생산성 향상 — 사내 지식이 개발자 손끝에.

---

## 요약 표

| # | 시나리오 | 핵심 building block (파일/tool) | 난이도 | 기대효과 |
|---|----------|-------------------------------|--------|----------|
| 1 | 코드 리뷰 자동화 | `search_context`, `get_graph_context`(양방향 BFS), `ast_code_extractor`, DECISIONS 인덱싱 | 🟡 | 사내 맥락 반영 근거 있는 리뷰 초안 |
| 2 | 설계/기술 문서 생성 | `/api/graph/explore`, `get_document(graph/chunks)`, AST 시그니처, `format_schema_for_llm` | 🟡 | 코드 파생 정확 문서로 공백 해소 |
| 3 | 기술 부채 분석 | `graph_store.graph`(DiGraph), `/api/graph/merges`, 청크 스캔, `get_schema_summary` | 🟡 | 그래프 근거 리팩터링 우선순위화 |
| 4 | 온보딩 자료 생성 | `search_context`, `/api/graph/full`(허브), `get_connected_component` | 🟢 | 항상 최신 자동 온보딩 가이드 |
| 5 | 버그 triage | `search_context`, `get_graph_context`, 임베딩 fallback | 🟢 | 원인 코드/문서를 즉시 좁힘 |
| 6 | 변경 영향 테스트 + 생성 | `imports`/`contains` 그래프, `import_symbols` label, AST docstring | 🟡 | 영향 집중 + 누락 케이스 보완 |
| 7 | 커밋/PR/체인지로그 | `content_hash` 변경감지, `search_context`, `get_graph_context`(scope) | 🟡 | "why" 담긴 메시지 자동화 |
| 8 | 검색 품질 회귀 게이트 | `build_synthetic_gold_set.py`, `eval_search.py`, `eval/metrics`·`graph_match` | 🟢 | 모든 자동화의 품질 가드레일 |
| 9 | 아키텍처 표류 감지 | LLM 본문 그래프 ↔ AST 그래프 + 정규 병합, `/api/graph/node` | 🔴 | 설계 의도-구현 괴리 자동 노출 |
| 10 | 사내 맥락 MCP 어시스트 | MCP 4 tools 전체(stdio), `assemble_context_with_sources` | 🟢 | 최저비용·최대 일상 생산성 |

---

## 공통 gap (전 시나리오 관통)

코드 분석에서 반복 확인된, 여러 시나리오의 정밀도를 동시에 끌어올릴 **핵심 보강 후보**:

1. **함수 호출(call) 그래프 부재** — 현재 그래프는 `imports`(파일→모듈)와 `contains`(클래스→메서드)만. `ast_code_extractor`에 함수 본문의 호출식(`ast.Call`) 분석을 추가하면 시나리오 1·3·6의 영향 분석 정밀도가 크게 향상된다. (Python은 AST로 비교적 저비용.)
2. **diff → 심볼 매핑 어댑터** — 시나리오 1·6·7이 공유하는 글루. `CodeSymbol.line_start/line_end`가 이미 있어 비교적 얇게 구현 가능.
3. **엔티티명 정합 보강** — 설계문서 자연어 엔티티명과 코드 FQN의 병합 신뢰도(시나리오 9). 임베딩 기반 별칭 매칭은 `search_entities_by_embedding`로 토대가 있음.
