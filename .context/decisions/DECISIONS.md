# 설계 결정 기록

결정 사항을 번호순으로 기록한다. 한번 결정된 사항은 삭제하지 않고 변경 시 새 항목으로 추가한다.

---

## D-001: 배포 형태 — CLI + 웹 UI

- **일시**: 2026-03-01
- **맥락**: Desktop App, Docker, CLI+WebUI 세 가지 후보 중 선택
- **결정**: CLI + 웹 UI (pip install 배포)
- **이유**: 부서원 기술 수준이 중간 이상이고, Python 환경이 이미 있는 경우가 많아 진입 장벽이 낮음. Streamlit/Gradio로 빠르게 프로토타이핑 가능.

---

## D-002: 인증 방식 — API Token (OAuth 대신)

- **일시**: 2026-03-01
- **맥락**: Confluence 연동 시 OAuth vs API Token
- **결정**: API Token (Basic Auth for Cloud, PAT for Data Center)
- **이유**: 서버 측 OAuth 앱 등록이 불필요. 콜백/리다이렉트 처리 없이 구현이 단순. 사용자 온보딩도 "토큰 발급 → 붙여넣기"로 간단.

---

## D-003: 토큰 저장 — OS keyring

- **일시**: 2026-03-01
- **맥락**: API 토큰을 config 파일에 평문 저장 vs keyring
- **결정**: OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
- **이유**: 설정 파일에 시크릿 노출 방지. keyring 라이브러리로 크로스 플랫폼 지원.

---

## D-004: 프로젝트 방향 — 대시보드 중심으로 전환

- **일시**: 2026-03-04
- **맥락**: 기존 CLI 중심 설계에서 비개발자 포함 사용 편의성 재검토
- **결정**: 웹 대시보드를 모든 조작(입력, 조회, 탐색)의 주 인터페이스로 전환
- **이유**: 문서 입력, 시각적 탐색, 그래프 시각화 등 대부분의 기능이 GUI에서 더 직관적. CLI는 MCP 서버 실행 등 자동화 목적으로만 유지.

---

## D-005: LLM 기반 저장 방식 자동 판단

- **일시**: 2026-03-04
- **맥락**: 모든 문서를 동일하게 텍스트 청크로만 저장할지, 문서 특성에 따라 저장 방식을 분리할지
- **결정**: LLM이 문서 내용을 분석하여 텍스트 청크(벡터DB) / 그래프DB / 혼합 중 최적 방식을 자동 결정
- **이유**: 서술형 문서는 청크 검색이 효과적이나, 엔티티 간 관계가 핵심인 문서(아키텍처, 조직도 등)는 그래프 탐색이 더 적절. 문서마다 수동으로 판단하는 것은 비현실적이므로 LLM에 위임.

---

## D-006: MCP Server 추가

- **일시**: 2026-03-04
- **맥락**: 저장된 사내 지식을 대시보드 외에 사내 LLM 애플리케이션에서도 활용할 방법 필요
- **결정**: MCP(Model Context Protocol) Server로 동작하여 stdio/SSE 전송을 통해 사내 LLM 앱이 질의 가능하도록 구현
- **이유**: Claude Code, 커스텀 에이전트 등 사내 LLM 도구에서 표준화된 프로토콜로 사내 지식 컨텍스트를 검색·조립하여 가져올 수 있어야 실질적 활용도가 높아짐.

---

## D-007: 문서 변경 재처리 전략 — Delete & Recreate

- **일시**: 2026-03-04
- **맥락**: 원본 문서가 변경되면 기존 청크/그래프 데이터를 어떻게 갱신할지
- **결정**: 변경 감지 시 해당 문서의 기존 파생 데이터를 전부 삭제 후 재생성 (Delete & Recreate)
- **이유**: 부분 업데이트(diff 기반)는 청크 경계 이동, 그래프 엔티티 변동 등이 복잡하고 불일치 위험이 큼. 전체 삭제 후 재생성이 구현이 단순하고 데이터 일관성 보장. 재처리 시 LLM Classifier도 재판정하므로 저장 방식 자체가 바뀌는 경우도 자연스럽게 대응.

---

## D-008: 웹 프레임워크 — FastAPI + Jinja2 + HTMX

- **일시**: 2026-03-11
- **맥락**: 웹 대시보드 프레임워크로 FastAPI+Jinja2 vs Streamlit 중 선택 (I-001)
- **결정**: FastAPI + Jinja2 + HTMX + Alpine.js + Pico CSS
- **이유**: 커스터마이징 자유도가 높고, MCP 서버와 같은 프로세스에서 실행 가능. HTMX로 SPA 수준의 인터랙션을 서버 렌더링으로 구현. Streamlit은 커스텀 UI 제한이 크고 별도 프로세스 필요.

---

## D-009: 그래프 시각화 — vis.js

- **일시**: 2026-03-11
- **맥락**: 인터랙티브 그래프 시각화 라이브러리로 vis.js vs D3.js 중 선택 (I-002)
- **결정**: vis.js (CDN)
- **이유**: 네트워크 그래프에 특화되어 노드/엣지 렌더링, 줌/드래그/클릭 이벤트가 기본 제공. D3.js 대비 구현 공수가 현저히 적음. 현재 요구사항(엔티티-관계 그래프 시각화)에 충분.

---

## D-010: LLM·임베딩 모델 연동 방식 — Endpoint 방식 전환 (자체 모델 서버)

- **일시**: 2026-03-13
- **맥락**: 기존 OpenAI/Anthropic API Key 방식으로 구현되어 있었으나, 자체 호스팅 모델 서버(vLLM, Ollama, TEI 등)를 사용할 예정으로 변경 필요
- **결정**: LLM과 임베딩 모두 OpenAI 호환 엔드포인트 URL 방식(`"endpoint"` provider)을 기본으로 채택. 기존 OpenAI/Anthropic API Key 방식도 하위 호환으로 유지.
- **구현 내용**:
  - `llm_client.py`: `EndpointLLMClient` 추가 — OpenAI 호환 API의 `base_url` 파라미터로 자체 서버 URL 지정
  - `embedder.py`: `EndpointEmbeddingClient` 추가 — 동일 방식
  - `config/default.yaml`: `llm.provider: "endpoint"`, `llm.endpoint`, `llm.api_key` 필드 추가; `processor.embedding_provider: "endpoint"`, `processor.embedding_endpoint`, `processor.embedding_api_key` 필드 추가
  - `web/api/documents.py`: `_run_pipeline`에서 `"endpoint"` provider 분기 처리
- **이유**: OpenAI SDK의 `base_url` 파라미터를 활용하면 vLLM, Ollama, HuggingFace TGI, TEI 등 OpenAI 호환 인터페이스를 제공하는 모든 자체 모델 서버와 동일한 코드로 연동 가능. API Key 없이도 동작하므로 사내 배포 시 보안 관리 부담 경감.

---

## D-011: LLM·임베딩 클라이언트 의존성 주입(DI) 전환 + Embedding langchain_core 표준화

- **일시**: 2026-03-13
- **맥락**: 기존에는 `_run_pipeline` 내부에서 config를 읽어 LLM/임베딩 클라이언트를 직접 생성했음. 향후 LLM·임베딩을 Agent로 구성하여 교체할 예정이므로 외부 주입 방식이 필요. 임베딩은 langchain 생태계와 통합 가능하도록 표준 인터페이스 채택이 필요.
- **결정**:
  1. 앱 시작(lifespan)에서 LLM·임베딩 클라이언트를 한 번 생성하여 `app.state`에 저장, FastAPI DI로 주입
  2. 임베딩 클라이언트를 `langchain_core.embeddings.Embeddings` 상속으로 재구현, httpx REST 직접 호출 방식 채택
- **구현 내용**:
  - `app.py`: lifespan에 `_build_llm_client`, `_build_embedding_client` 팩토리 함수 추가, `app.state.llm_client` / `app.state.embedding_client` 저장
  - `dependencies.py`: `get_llm_client`, `get_embedding_client` DI 함수 추가
  - `documents.py`: `trigger_processing`에서 클라이언트를 DI로 주입받아 `_run_pipeline`에 전달. 클라이언트 생성 로직 완전 제거
  - `embedder.py`: `EmbeddingClient` ABC 제거. `EndpointEmbeddingClient(Embeddings)` — httpx로 OpenAI 호환 REST 직접 호출, 동기/비동기 모두 구현. `LocalEmbeddingClient(Embeddings)` 동일 인터페이스로 재구현
  - `pipeline.py`: `EmbeddingClient` → `Embeddings` 타입으로 교체, `embed()` → `aembed_documents()` 호출
  - `pyproject.toml`: `langchain-core>=0.3.0` 의존성 추가
- **이유**: DI 방식으로 전환하면 클라이언트 구현체를 Agent 기반 등으로 자유롭게 교체 가능. `langchain_core.embeddings.Embeddings` 인터페이스를 따르면 langchain 생태계의 다양한 임베딩 구현체(OpenAIEmbeddings, HuggingFaceEmbeddings 등)와 바로 교환 가능. httpx 직접 호출은 OpenAI SDK 의존 없이 순수 REST로 동작해 서버 호환성이 더 넓음.

---

## D-012: MCP Server — FastMCP + context_assembler 아키텍처

- **일시**: 2026-03-16
- **맥락**: Phase 5에서 MCP Server를 구현할 때, MCP SDK 활용 방식과 컨텍스트 검색 로직의 재사용성을 결정해야 함
- **결정**: FastMCP 기반 서버 + context_assembler 모듈 분리. 벡터 검색과 그래프 탐색을 결합하는 로직을 `context_assembler.py`로 분리하여 MCP Tool과 웹 채팅 API 모두에서 재사용.
- **구현 내용**:
  - `mcp/server.py`: FastMCP 서버 메인 — stdio/SSE 전송, 저장소 초기화
  - `mcp/tools.py`: 4개 MCP Tool 등록 (search_context, list_documents, get_document, get_graph_context)
  - `mcp/context_assembler.py`: 벡터 검색 + 그래프 탐색 결과 병합·포맷팅, 출처 정보 추출
  - `pyproject.toml`: `mcp>=1.0.0` 의존성 추가
- **이유**: context_assembler를 독립 모듈로 분리하면 MCP Tool과 웹 API 양쪽에서 동일한 검색 로직을 호출할 수 있어 코드 중복 방지. FastMCP는 Python SDK에서 제공하는 고수준 API로 Tool 등록이 데코레이터 한 줄로 가능.

---

## D-013: 채팅 인터페이스 — Alpine.js + JSON API 방식

- **일시**: 2026-03-16
- **맥락**: Phase 6에서 대시보드 내 채팅 인터페이스 구현 시 프론트엔드 접근 방식 선택 필요. HTMX SSE 스트리밍 vs Alpine.js + JSON API fetch 중 선택.
- **결정**: Alpine.js 기반 클라이언트 + POST /api/chat JSON API 방식 채택. 스트리밍 없이 전체 응답을 한 번에 반환.
- **구현 내용**:
  - `web/api/chat.py`: 채팅 API 엔드포인트 — RAG 파이프라인 (context_assembler 재사용 → LLM 호출 → 답변 + 출처 반환)
  - `templates/chat.html`: Alpine.js x-data로 메시지 상태 관리, 출처 링크 표시
  - `static/js/chat.js`: chatApp() Alpine 컴포넌트 — fetch API 호출, 메시지 렌더링
  - `context_assembler.py`: `assemble_context_with_sources()` 함수 추가 — 출처 정보(document_id, title, similarity) 포함 반환
- **이유**: 현재 LLM API가 스트리밍을 필수로 하지 않으므로 전체 응답 방식이 구현 단순. 출처 표시를 위해 JSON 구조가 필요하므로 HTMX 파셜보다 JSON API가 적합. Alpine.js는 이미 프로젝트에서 사용 중이라 추가 의존성 없음.

---

## D-014: 그래프 탐색 개선 — 임베딩 기반 엔티티 매칭 (C안)

- **일시**: 2026-03-16
- **맥락**: 기존 그래프 탐색은 `query.split()` → 엔티티 이름 완전 일치 방식으로, "게이트웨이"→"Gateway" 매칭 불가, 질의 의도와 무관하게 항상 실행, depth 고정 등의 한계가 있었음. LLM 분류(A안), 임베딩 매칭(B안), 하이브리드(C안) 중 선택.
- **결정**: C안 (임베딩 기반 엔티티 매칭 + 매칭 유무로 그래프 탐색 자동 결정) 채택.
- **구현 내용**:
  - `graph_store.py`: `_entity_embeddings` 캐시 딕셔너리 추가 (node_id → (name, embedding)), `build_entity_embeddings()` — 전체 엔티티 이름 임베딩 생성 및 캐시, `search_entities_by_embedding()` — 코사인 유사도 기반 엔티티 검색 (threshold, top_k), `_cosine_similarity()` 유틸 함수
  - `context_assembler.py`: `_search_graph_by_embedding()` — 질의 임베딩 → 엔티티 매칭 → 이웃 탐색 → 포맷팅. 매칭 엔티티 없으면 None 반환(탐색 스킵). 매칭 수에 따라 depth 동적 결정 (3개 이상: depth=1, 미만: depth=2). 질의 임베딩을 벡터 검색과 엔티티 매칭에 공용으로 재사용.
  - `app.py`: lifespan에서 `build_entity_embeddings()` 사전 빌드. save/delete 시 캐시 자동 무효화.
- **이유**: LLM 추가 호출 없이 기존 임베딩 인프라를 재활용. 다국어/유사어 매칭 가능 ("게이트웨이"↔"Gateway"). 매칭 엔티티가 없으면 그래프 탐색을 자연스럽게 스킵하여 불필요한 컨텍스트 노이즈 방지.
- **후속**: D-015에서 LLM 기반 탐색 플래너로 교체됨.

---

## D-015: 그래프 탐색 개선 — LLM 기반 탐색 플래너

- **일시**: 2026-03-16
- **맥락**: D-014 임베딩 매칭은 다국어 매칭이 가능하지만 질의의 의미적 의도를 충분히 반영하지 못함. LLM이 그래프 구조를 이해하고 탐색 계획을 세우는 방식이 더 정확.
- **결정**: LLM 기반 그래프 탐색 플래너 방식 채택.
- **구현 내용**:
  - `graph_store.py`: `get_schema_summary()` — 엔티티/관계 유형별 집계, 대표 엔티티 목록, 관계 예시 반환. `format_schema_for_llm()` — LLM 프롬프트용 마크다운 포맷.
  - `graph_search_planner.py` (신규): `plan_graph_search()` — LLM에 스키마 + 질의 전달 → JSON 탐색 계획. `execute_graph_search()` — 계획의 step(entity_name, depth, focus_relations)에 따라 탐색. `_parse_plan()` — depth 1~2 제한, steps 최대 3개.
  - `context_assembler.py`: `_search_graph_with_llm()` — plan → execute 파이프라인. `llm_client` 파라미터 추가 (None이면 그래프 탐색 스킵).
  - `chat.py`, `tools.py`, `server.py`: llm_client 전달 연동.
- **이유**: LLM이 질의 의도를 분석하여 탐색 여부/시작 엔티티/깊이/관계 유형을 결정. 임베딩 매칭보다 정밀한 그래프 컨텍스트 추출 가능. 그래프가 비어있으면 LLM 호출 없이 스킵.

---

## D-016: Confluence 문서 입력 — MCP Client 방식 채택 (REST API 대체)

- **일시**: 2026-03-24
- **맥락**: Confluence REST API를 통한 사내 문서 임포트가 API 접근 제한으로 불가능한 상황. 사내에 Confluence MCP Server가 이미 운영 중이며, `searchContent`, `getPage`, `getChild`, `getSpaceInfoAll`, `getUserContributedPages` 등 9종의 도구를 제공. 현재 시스템은 `mcp>=1.0.0` 라이브러리를 MCP Server 역할로 이미 사용 중.
- **결정**: 사내 Confluence MCP Server에 MCP Client로 연결하여 문서를 가져오는 4번째 입력 경로 추가.
- **구현 계획**:
  - `ingestion/mcp_confluence.py` (신규): MCP 클라이언트 모듈. `mcp.ClientSession` + SSE/stdio 전송으로 사내 MCP 서버에 연결. `searchContent` → `getPage` 흐름으로 문서 본문 수신 후 MetadataStore에 `source_type="confluence_mcp"`로 저장.
  - `web/api/confluence_mcp.py` (신규): 웹 API 엔드포인트. 연결 테스트, 검색, 스페이스 탐색, 페이지 임포트 등.
  - `config/default.yaml`: `sources.confluence_mcp` 섹션 추가 (transport, server_url, sync_interval).
  - 탭 기반 통합 UI: 검색 탭 (searchContent), 탐색 탭 (getSpaceInfoAll + getChild), 내 문서 탭 (getUserContributedPages).
- **사용자 시나리오**: 3가지 임포트 방법 제공.
  - 1순위 — 검색 기반: `searchContent(query)` → 결과에서 선택 → `getPage(id)` × N
  - 2순위 — 트리 탐색: `getSpaceInfoAll()` → `getChild(pageId)` 재귀 → `getPage(id)` × N
  - 3순위 — 내 문서: `getUserContributedPages(userId)` → 선택 → `getPage(id)` × N
- **이유**: 추가 라이브러리 설치 불필요 (`mcp` 이미 의존성에 포함). 인증/권한은 MCP 서버 측에서 처리하므로 클라이언트 구현 단순. 기존 ingestion 패턴(`async` 함수 → `dict` 반환)을 그대로 따르므로 ProcessingPipeline 변경 불필요. REST API 직접 접근이 차단된 환경에서 유일한 Confluence 연동 경로.

---

## D-018: HTML→Markdown 변환기 — BeautifulSoup 전처리 + markdownify 공유 모듈

- **일시**: 2026-03-30
- **맥락**: `confluence.py`의 `_html_to_markdown()`이 정규식 기반이라 테이블, Confluence 매크로, 중첩 목록이 모두 소실됨 (I-012, I-005). `mcp_confluence.py`는 D-017에서 markdownify로 전환했지만 매크로 전처리 없이 단순 변환만 수행. 두 모듈의 변환 로직이 분리되어 있어 품질 차이 발생.
- **결정**: BeautifulSoup으로 Confluence 매크로를 표준 HTML로 전처리한 뒤 markdownify로 최종 변환하는 공유 모듈 `ingestion/html_converter.py`를 신규 생성. `confluence.py`와 `mcp_confluence.py` 양쪽에서 이 모듈에 위임.
- **구현 내용**:
  - `html_converter.py`: `html_to_markdown()` 함수 — `_preprocess_confluence_macros()` → `markdownify()` → `_postprocess()` 파이프라인
  - Confluence 매크로 전처리: info/warning/note/tip 패널 → blockquote, code/noformat → 코드 블록, expand → 본문 펼침, toc/children → 제거, ac:image → img, ac:link → a
  - `confluence.py`: `_html_to_markdown()` → `html_to_markdown()` 위임
  - `mcp_confluence.py`: `convert_html_to_markdown()` → 동일 모듈 위임, `markdownify` 직접 import 제거
  - `pyproject.toml`: `beautifulsoup4>=4.12.0` 의존성 추가
- **이유**: 공유 모듈로 변환 품질을 일원화하고, BeautifulSoup 전처리로 Confluence 전용 매크로를 표준 HTML로 정규화한 뒤 markdownify에 넘기면 테이블·목록·서식 등 기본 HTML 변환은 라이브러리가 처리. 정규식 기반 대비 테이블 구조 보존, 매크로 내용 보존, 중첩 목록 정상 변환이 가능해져 원본 데이터 품질이 대폭 향상.

---

## D-019: 청킹 전략 — 마크다운 헤딩 기반 계층적 분할 + section_path 메타데이터

- **일시**: 2026-03-30
- **맥락**: 기존 청커(`chunker.py`)가 `\n\n` 기준으로만 단락을 분리하여 마크다운 헤딩 구조를 무시 (I-013). `# 섹션 제목`과 본문이 서로 다른 청크로 분리되어 검색 시 컨텍스트 소실. 메타데이터에 `chunk_index`와 `title`만 저장되어 해당 청크가 문서의 어느 위치에 속하는지 알 수 없음.
- **결정**: 마크다운 헤딩(`#`~`######`)을 인식하여 섹션별로 분리한 뒤 각 섹션 내에서 토큰 기반 청킹 수행. 각 청크에 상위 헤딩 경로(`section_path`)를 자동 첨부.
- **구현 내용**:
  - `chunker.py`: `Chunk` dataclass에 `section_path: str` 필드 추가. `_split_into_sections()` — 헤딩 스택으로 계층 경로 계산. `_chunk_section()` — 기존 토큰 기반 분할 로직을 섹션 단위로 적용. 헤딩 텍스트를 청크 본문에 포함하여 임베딩 시 검색 정확도 향상. 헤딩 없는 문서는 기존과 동일하게 동작 (하위 호환).
  - `pipeline.py`: 벡터스토어 메타데이터에 `section_path` 포함.
  - `context_assembler.py`: `_format_chunk_results()`와 `assemble_context_with_sources()`에서 검색 결과에 섹션 경로 표시.
- **이유**: 헤딩을 청크 본문에 포함하면 임베딩에 섹션 제목의 의미가 반영되어 "백엔드 기술 스택" 같은 쿼리와의 유사도가 높아짐. `section_path` 메타데이터를 통해 LLM이 답변 생성 시 문서 내 위치를 파악할 수 있고, 사용자에게 출처를 섹션 수준으로 안내 가능. 섹션 경계에서 분할하면 서로 다른 주제의 텍스트가 한 청크에 섞이는 문제도 방지.

---

## D-020: Cross-encoder Reranker + 유사도 Threshold — LLM 기반 리랭킹

- **일시**: 2026-03-31
- **맥락**: 벡터 검색(bi-encoder)이 top-K 결과를 반환하지만 유사도 threshold가 없어 관련 없는 청크도 포함됨 (I-015). 벡터 검색 + 그래프 탐색 결과가 단순 concat으로 병합되어 중복/무관 정보의 우선순위 조정이 불가능. Cross-encoder 기반 정밀 재평가가 없어 bi-encoder의 recall은 높지만 precision이 낮음.
- **결정**: 두 가지 메커니즘을 도입.
  1. **유사도 threshold**: `_search_chunks()`에서 cosine similarity가 설정값(`search.similarity_threshold`, 기본 0.3) 미만인 청크를 제외.
  2. **LLM 기반 리랭커**: `processor/reranker.py` 신규 생성. 벡터 검색 결과를 LLM에 한꺼번에 전달하여 0~10점으로 관련도를 평가받고, 점수순으로 재정렬. 점수 threshold(`search.reranker_score_threshold`) 미만 청크 추가 제외.
- **구현 내용**:
  - `processor/reranker.py`: `rerank()` 함수 — 단일 LLM 호출로 전체 청크 평가 (비용 최소화). LLM 실패 시 원본 순서 유지 (graceful degradation). `_parse_scores()` — JSON 응답 파싱 + 점수 클램핑(0~10).
  - `mcp/context_assembler.py`: `assemble_context()`, `assemble_context_with_sources()` 에 `similarity_threshold`, `rerank_enabled`, `rerank_top_k`, `rerank_score_threshold` 파라미터 추가. 벡터 검색 → threshold 필터링 → 리랭킹 → 점수 threshold 필터링 → 포맷팅 파이프라인.
  - `mcp/tools.py`: MCP search_context 도구에서 config 기반 설정 전달.
  - `web/api/chat.py`: 웹 채팅 API에서 config 기반 설정 전달.
  - `config/default.yaml`: `search` 섹션 추가 (`similarity_threshold: 0.3`, `reranker_enabled: false`, `reranker_top_k: 5`, `reranker_score_threshold: 4.0`).
- **이유**: 유사도 threshold는 무관한 청크를 사전에 필터링하여 LLM 컨텍스트 창의 노이즈를 줄임. LLM 기반 리랭커는 cross-encoder 전용 모델을 추가 설치하지 않고 기존 LLM 인프라를 활용하여 bi-encoder의 precision 한계를 극복. 단일 호출로 모든 청크를 한꺼번에 평가하여 N번 호출 대비 비용/지연 최소화. 리랭커는 설정(`reranker_enabled`)으로 비활성화 가능하여 LLM 없는 환경에서도 문제 없음.

---

## D-021: 그래프 추출 — Map-reduce 방식 전체 문서 처리

- **일시**: 2026-03-31
- **맥락**: `graph_extractor.py`의 `extract_graph()`가 `content[:4000]`으로 앞 4000자만 LLM에 전달 (I-014). Confluence 문서는 도입부가 목차/배경이고 핵심 아키텍처 정보가 후반부에 있는 경우가 많아, 후반부 엔티티/관계가 모두 누락됨.
- **결정**: Map-reduce 방식으로 전체 문서를 분할 추출 후 병합.
  - **Map**: 문서를 `max_content_chars` 크기로 분할하여 각 청크에서 독립적으로 그래프 추출. 분할 시 단락 경계(`\n\n`) → 줄바꿈(`\n`) → 강제 분할 순서로 자연스러운 경계 존중.
  - **Reduce**: 전체 결과를 병합. 엔티티는 `(name.lower(), entity_type)` 기준 중복 제거, 설명이 비어 있으면 나중 것으로 보충. 관계는 `(source.lower(), target.lower(), relation_type)` 기준 중복 제거.
- **구현 내용**:
  - `graph_extractor.py`: `extract_graph()` — 문서 길이에 따라 자동 분기 (짧으면 기존 단일 호출, 길면 map-reduce). `_extract_map_reduce()` — 청크별 LLM 호출 + 부분 실패 허용(graceful). `_split_content()` — 자연 경계 존중 분할. `_merge_graphs()` — 대소문자 무시 중복 제거. `_parse_graph_response()` — 공통 파싱 로직 추출.
  - `_USER_PROMPT_CHUNK_TEMPLATE` — 청크 프롬프트에 `(N/M)` 섹션 정보 포함하여 LLM이 문서 위치를 인식.
  - `pipeline.py` — 변경 없음 (함수 시그니처 동일, 내부에서 자동 분기).
- **이유**: 기존 4000자 제한은 문서 후반부 정보를 완전히 무시함. Map-reduce로 전체 문서를 처리하면 10,000자+ 문서에서도 모든 엔티티/관계를 추출 가능. 부분 실패 허용으로 한 청크의 LLM 호출이 실패해도 나머지 결과를 살릴 수 있음. 중복 제거로 여러 청크에서 동일 엔티티가 추출되어도 그래프가 깨끗하게 유지됨. 짧은 문서는 기존과 동일하게 단일 호출(하위 호환).

---

## D-022: 쿼리 확장 — HyDE (Hypothetical Document Embedding)

- **일시**: 2026-03-31
- **맥락**: 사용자 쿼리가 그대로 임베딩되어 벡터 검색에 사용됨 (I-016). 짧은 질의("배포 절차")와 문서 청크("릴리즈 프로세스는 CI/CD 파이프라인을 통해…") 사이에 어휘적 갭(lexical gap)이 존재하여 의미적으로 관련된 문서를 놓침. 동의어, 약어, 기술 용어 차이로 인한 검색 재현율(recall) 저하.
- **결정**: HyDE(Hypothetical Document Embedding) 방식 도입. LLM에게 질의에 대한 가상 답변 문서를 생성시킨 뒤, 원본 쿼리 임베딩과 가상 문서 임베딩을 평균하여 의미적으로 풍부한 검색 벡터를 생성.
- **구현 내용**:
  - `processor/query_expander.py` (신규): `generate_hypothetical_document()` — LLM에게 3~5문장의 가상 답변 단락 생성 요청 (temperature=0.7로 다양성 확보). `expand_query_embedding()` — 원본 쿼리 임베딩 + HyDE 임베딩 평균 벡터 반환. HyDE 실패 시 원본 임베딩 반환 (graceful degradation).
  - `mcp/context_assembler.py`: `assemble_context()`, `assemble_context_with_sources()`에 `hyde_enabled` 파라미터 추가. 활성화 시 `_embed_query()` 대신 `expand_query_embedding()` 사용.
  - `mcp/tools.py`, `web/api/chat.py`: config에서 `search.hyde_enabled` 읽어 전달.
  - `config/default.yaml`: `search.hyde_enabled: false` 추가 (기본 비활성).
- **이유**: HyDE는 질의를 문서 공간으로 변환하여 어휘적 갭을 해소. "배포 절차" 질의 → 가상 문서에 "릴리즈", "디플로이", "CI/CD", "파이프라인" 등 동의어가 자연스럽게 포함 → 이 용어들이 포함된 청크와의 유사도 상승. 원본 임베딩과 평균하여 원래 질의의 의도도 보존. LLM 1회 추가 호출(512토큰)로 구현되어 비용 낮음. 설정으로 비활성화 가능.

---

## D-023: 문서 분류기 입력 범위 확대 — 시작/중간/끝 구간 샘플링

- **일시**: 2026-04-01
- **맥락**: `classifier.py`가 `content[:2000]`으로 앞 2000자만 LLM에 전달 (I-017). 문서 전체 구조를 파악하지 못해 잘못된 분류 발생 가능. 예: 앞부분은 서술형 배경 설명이지만 후반부에 아키텍처 다이어그램과 엔티티 관계가 있는 경우 `chunk`로 잘못 분류될 수 있음.
- **결정**: 짧은 문서(4000자 이하)는 전문을 전달하고, 긴 문서는 시작/중간/끝 세 구간에서 균등 샘플링하여 총 ~4000자를 LLM에 전달.
- **구현 내용**:
  - `classifier.py`: `_sample_content()` 함수 추가 — 4000자 이하 전문 반환, 초과 시 시작(1300자) + 중간(1300자) + 끝(1300자) 샘플링. 프롬프트에 `[Beginning]/[Middle]/[End]` 레이블과 총 문자 수 포함.
  - `_USER_PROMPT_TEMPLATE` 변경 — 고정 `Content (first 2000 chars)` → 동적 `content_label` (전문/샘플링 여부 표시).
  - 기존 테스트 호환 유지 (짧은 content는 기존과 동일하게 동작).
- **이유**: 시작/중간/끝 샘플링은 단순하면서도 문서의 구조적 다양성을 포착. 도입부(배경/목차) + 본문 핵심(중간) + 결론/참고(끝)를 모두 볼 수 있어 chunk vs graph vs hybrid 판정 정확도 향상. LLM 호출 수 증가 없이 입력 품질만 개선.

---

## D-024: 크로스-문서 엔티티 병합 — 정규 노드 + 조인 테이블

- **일시**: 2026-04-01
- **맥락**: `graph_store.py`에서 동일 엔티티가 문서마다 별도 노드로 생성됨 (I-018, I-003). 문서 A의 "쿠버네티스"와 문서 B의 "쿠버네티스"가 별도 노드로 존재하여 크로스-문서 관계 탐색이 불완전하고, 스키마 요약/임베딩 검색에서 중복 발생.
- **결정**: 정규 노드(canonical node) 방식 — `entity_name(대소문자 무시) + entity_type` 기준으로 동일 엔티티는 하나의 노드로 병합. `graph_node_documents` 조인 테이블로 노드-문서 다대다 관계 관리.
- **구현 내용**:
  - `metadata_store.py`: `graph_node_documents` 테이블 추가 (node_id, document_id). `find_graph_node_by_entity()`, `add_node_document_link()`, `get_all_node_document_links()` 등 메서드 추가. `delete_graph_data_by_document()` 수정 — 연결 해제 + 고아 노드 정리.
  - `graph_store.py`: `save_graph_data()` — 기존 정규 노드 검색 후 재사용/신규 생성 분기. description 보강. `load_from_db()` — `graph_node_documents`에서 document_ids 로드. `delete_document_graph()` — 부분 해제, 고아 노드만 삭제. NetworkX 노드에 `document_ids` (set) 속성 저장.
  - `graph_search_planner.py`: `execute_graph_search()`에서 `document_ids` set 대응.
- **이유**: 정규 노드 방식은 그래프가 깔끔하고 중복이 없어 LLM 스키마 요약 품질 향상. 임베딩 검색 시 top_k 자리 낭비 방지. 문서 삭제 시 `graph_node_documents` 연결만 해제하고 고아 노드만 정리하여 안전. "same_as" 엣지 방식 대비 그래프 복잡도 낮음.

---

## D-025: 코드 기반 컨텍스트 구축 — LLM 문서 생성 + 원본 코드 하이브리드

- **일시**: 2026-04-02
- **맥락**: Confluence 문서만으로는 사내 기술 컨텍스트가 부족. 실제 개발 코드, 커밋 히스토리, PR 리뷰 등 기술적 맥락이 시스템에 없음. 코드를 컨텍스트로 변환하는 방식으로 (A) 코드→LLM 문서 생성→기존 파이프라인과 (B) 코드→AST 파싱→직접 Chunking 두 가지를 비교 검토.
- **결정**: A + B 하이브리드 방식 채택.
  - **접근 A (code_doc)**: 코드를 LLM에 전달하여 자연어 문서를 생성. `source_type = "code_doc"`. 기존 chunker + graph_extractor 그대로 재사용. "왜 이렇게 구현했는가" 등 설계 의도 포함.
  - **접근 B (git_code)**: 원본 코드를 파일 단위로 저장. `source_type = "git_code"`. 환각 검증 및 정확한 코드 참조용.
- **접근 A 선택 이유**:
  - LLM이 소비하는 컨텍스트는 자연어가 토큰 대비 정보 밀도가 높음
  - "왜"가 포함된 컨텍스트가 없으면 LLM이 매번 추론해야 함. 수집 시점에 한 번만 추론
  - 기존 파이프라인(마크다운 헤딩 chunking, LLM 그래프 추출) 100% 재사용 가능
  - 비개발자(PM, 기획자)도 자연어 문서로 기술 컨텍스트에 접근 가능
- **접근 B 병행 이유**:
  - LLM 문서 생성의 환각 위험을 원본 코드로 검증
  - 정확한 코드 라인을 참조해야 하는 개발자 유스케이스 지원
- **검색 시 동작**: code_doc chunk 반환(이해용) → document_sources 테이블로 원본 git_code 조회(검증용) → LLM에 둘 다 제공
- **추가 컨텍스트 소스 우선순위** (ROI 기준):
  1. Git Repository (코드 + 커밋 + PR) — 사내 기술 컨텍스트의 60-70% 커버
  2. Jira/이슈 트래커 — 요구사항-코드 연결
  3. API 명세 (OpenAPI) — Git에 포함 시 1순위와 동시 해결
  4. DB 스키마 — 도메인 모델의 실체
  5. Slack/Teams — 가치 높지만 노이즈 필터링 복잡
- **결론**: Git 먼저 구현 → Jira 두 번째 → 이 둘로 사내 컨텍스트의 80-90% 자동 유지 가능

---

## D-026: 코드 문서 ↔ 원본 코드 연결 — document_sources 테이블

- **일시**: 2026-04-02
- **맥락**: D-025 하이브리드 방식에서 `code_doc`(LLM 생성 문서)이 반환되었을 때 대응하는 원본 코드(`git_code`)를 찾아 검증하는 연결 메커니즘이 필요. 기존 스키마의 `UNIQUE(source_type, source_id)`로는 `source_id` 매칭만 가능하나, 실제 사내 코드에서 LLM 문서 1개가 여러 파일을 참조하는 경우가 일반적 (예: VPC 관련 코드 = vpc.tf + subnets.tf + nat.tf).
- **검토한 방법**:
  1. `source_id` 직접 매칭: 스키마 변경 없이 같은 source_id 공유. 단, 1파일=1문서일 때만 동작하여 다중 파일 참조 시 깨짐.
  2. `document_sources` 연결 테이블: doc_id → source_doc_id N:M 관계. 정확한 추적.
  3. 벡터 재검색: code_doc과 같은 쿼리로 git_code를 한 번 더 검색. 연결 관리 불필요하나, 정확한 코드를 못 찾을 수 있음.
- **결정**: 방법 2 — `document_sources` 연결 테이블 도입.
  ```sql
  CREATE TABLE document_sources (
      doc_id        INTEGER REFERENCES documents(id) ON DELETE CASCADE,
      source_doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
      file_path     TEXT,
      PRIMARY KEY (doc_id, source_doc_id)
  );
  ```
- **구현 계획**:
  - `metadata_store.py`: `document_sources` 테이블 추가. `add_document_source()`, `get_document_sources()`, `get_documents_by_source()` 메서드 추가.
  - `ingestion/git_repository.py` (신규): Git repo clone/pull → 파일별 git_code 문서 저장 → 관련 파일 그룹핑 → LLM 문서 생성 → code_doc 저장 + document_sources 연결 기록.
  - `context_assembler.py`: code_doc chunk 반환 시 document_sources 조회하여 원본 코드 첨부 옵션.
- **이유**: N:M 관계 지원으로 LLM이 여러 파일을 종합하여 하나의 문서를 생성하는 자연스러운 흐름을 정확히 추적 가능. 검색 시 code_doc → 원본 코드 역추적이 확실하여 환각 검증에 필수적.

---

## D-027: 멀티에이전트 기반 계층적 문서 생성

- **일시**: 2026-04-02
- **맥락**: 모노레포에 여러 상품의 코드가 혼재하며 코드량이 매우 많음. 단일 LLM으로 순차 처리 시 시간이 과다(파일 500개 × ~5초 = ~42분). 계층적 문서 생성(Level 1 파일 요약 → Level 2 디렉토리 문서 → Level 3 상품 문서)을 병렬화하여 처리 시간 단축 필요.
- **결정**: 3계층 멀티에이전트 아키텍처 채택.
  - **Coordinator Agent** (최상위): config 로드, 상품별 Product Agent 할당, 전체 진행 관리.
  - **Product Agent** (상품별): 파일 수집, 디렉토리 그룹핑, Worker 할당, Category Agent 할당.
  - **Worker Agent** (디렉토리별): Level 1 파일별 요약 + Level 2 디렉토리별 관점 중립 문서 생성. 모든 Category Agent가 이 결과를 공유.
  - **Category Agent** (카테고리별): Level 2 결과를 받아 특정 관점(아키텍처, 개발, 인프라 등)으로 Level 3 종합 문서 생성.
- **핵심 설계**: Worker는 관점 중립 사실 요약(1회 실행), Category Agent가 관점 부여(카테고리 수만큼 병렬 실행). 코드 분석 비용 중복 없음.
- **상품 스코프 결정**: config에 상품별 paths/exclude를 정의. 최초에 LLM이 레포 디렉토리 트리를 분석하여 스코프를 제안하고, 사람이 검토 후 확정. 이후 완전 자동.
- **Worker 단위**: 디렉토리 기반. 파일 30개 초과 시 서브디렉토리 분할, 3개 미만 시 상위와 병합.
- **증분 처리**: `git diff`로 변경 파일 감지 → 해당 상품의 영향받는 디렉토리만 Worker 재실행 → Category Agent 재실행. 미변경 상품/디렉토리는 건드리지 않음.
- **이유**: 병렬 처리로 ~42분 → ~2분 (속도 20배 향상, 비용 동일). 관점 중립 요약을 공유하여 Worker 비용 중복 방지. Coordinator → Product → Worker/Category 계층 구조로 확장성 확보.

---

## D-028: 상품 × 카테고리 매트릭스 문서 체계

- **일시**: 2026-04-02
- **맥락**: 같은 코드를 봐도 독자(아키텍트, 개발자, 인프라, 프라이싱, 사업)에 따라 필요한 문서가 다름. 상품 1개당 문서 1개가 아닌 상품 × 카테고리 매트릭스로 문서를 생성해야 함. 또한 팀마다 카테고리가 추가될 가능성이 있어 확장성 필요.
- **결정**: 카테고리를 config의 프롬프트로만 정의하여 코드 변경 없이 자유롭게 추가/수정 가능하도록 설계.
- **카테고리 정의 방식**: 각 카테고리는 `display_name`, `prompt` (LLM 지시), `target_audience`, `model` (엔드포인트 지정)로 구성. 카테고리 추가 시 config에 항목 1개 추가 + 프롬프트 작성만으로 전체 상품에 해당 관점 문서 자동 생성.
- **기본 카테고리**: architecture(아키텍처), development(개발 가이드), infrastructure(인프라 운영), pricing(과금 체계), business(사업 요약).
- **문서 저장 규칙**:
  - `source_type = "git_code"`: 원본 코드 파일 (파일 단위)
  - `source_type = "code_summary"`: Worker가 생성한 관점 중립 디렉토리 요약
  - `source_type = "code_doc"`: Category Agent가 생성한 관점별 문서
  - `source_id` 규칙: `"{product}:{category}"` (예: `"vpc:architecture"`, `"vpc:pricing"`)
  - `document_sources` (D-026)로 code_doc ↔ git_code 연결 추적
- **이유**: 카테고리를 프롬프트 기반으로 정의하면 새 팀이 추가되어도 코드 수정 없이 config만으로 대응 가능. 상품 × 카테고리 매트릭스는 조직의 다양한 역할을 커버하면서도 코드 분석(Worker)은 한 번만 수행하여 비용 효율적.

---

## D-029: 에이전트별 모델 계층화 — 엔드포인트 방식 통일

- **일시**: 2026-04-02
- **맥락**: D-027 멀티에이전트에서 각 에이전트의 역할별로 필요한 모델 성능이 다름. 파일 요약(Worker)은 경량 모델, 종합 문서(Category Agent)는 고성능 모델이 적합. 기존 시스템은 D-010에서 OpenAI 호환 엔드포인트 방식을 채택하여 자체 모델 서버를 사용 중. 모든 에이전트의 LLM 호출도 동일하게 엔드포인트 방식으로 통일해야 함.
- **결정**: 에이전트별로 다른 엔드포인트/모델을 지정할 수 있는 모델 계층화 구조. 모든 LLM 호출은 OpenAI 호환 엔드포인트 방식(D-010)으로 통일.
- **모델 배치**:
  - Worker Agent (Level 1 파일 요약): 경량 모델 (Haiku급) — `worker_endpoint`
  - Worker Agent (Level 2 디렉토리 문서): 중간 모델 (Sonnet급) — `synthesizer_endpoint`
  - Category Agent (Level 3 카테고리 문서): 고성능 모델 (Opus급) — `orchestrator_endpoint`
  - Coordinator (품질 검증): 중간 모델 (Sonnet급) — `synthesizer_endpoint`
- **config 구조**: `sources.git.processing` 하위에 에이전트별 `endpoint`, `model`, `api_key` 지정. 미지정 시 기존 `llm.endpoint` 폴백.
- **비용 예시** (파일 500개, 디렉토리 50개, 카테고리 5개, 상품 1개): Worker 500회(Haiku) + Synthesizer 50회(Sonnet) + Category 5회(Opus) + 검증 50회(Sonnet) → 모두 Opus 대비 80%+ 비용 절감.
- **이유**: 작업 복잡도에 맞는 모델을 배치하여 비용 최적화. 엔드포인트 방식 통일로 기존 `llm_client.py`의 `EndpointLLMClient`를 그대로 재사용. 에이전트별 엔드포인트를 분리하면 Worker는 빠르고 저렴한 자체 서버, Category Agent는 고성능 서버로 분배 가능.
