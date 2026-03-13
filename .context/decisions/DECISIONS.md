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
