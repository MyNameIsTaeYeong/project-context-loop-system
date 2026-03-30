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
