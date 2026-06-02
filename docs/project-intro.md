# Project Context Loop System

> 사내 지식(Confluence 문서·파일·소스 코드 등)을 **LLM 컨텍스트로 변환·저장**하고,
> 웹 대시보드로 시각적으로 관리하며, **MCP Server**로 사내 LLM 애플리케이션에
> 적절한 컨텍스트를 검색·조립하여 제공하는 시스템입니다.

---

## 한눈에 보기

| | 기능 | 설명 |
|---|------|------|
| 📥 | **다중 입력** | Confluence MCP · 파일 업로드 · 직접 작성 · Git 코드 수집 |
| 🧠 | **LLM 기반 저장 판단** | 문서 내용을 분석해 텍스트 청크 · 그래프 DB · 혼합 중 최적 방식을 자동 결정 |
| 🖥️ | **대시보드 중심** | 원본 문서와 변환된 데이터(청크·그래프 노드/엣지)를 시각적으로 탐색 |
| 🔌 | **MCP Server** | 사내 LLM 앱이 질의하면 벡터 검색 + 그래프 탐색으로 컨텍스트 조립·응답 |
| 🔁 | **자동 재처리** | 원본 변경을 `content_hash`로 감지하여 파생 데이터를 Delete & Recreate |
| 🔒 | **로컬 퍼스트 & 보안** | 모든 데이터는 사용자 PC에 저장, 인증 토큰은 OS 키체인(keyring)에 보관 |

---

## 시스템 아키텍처

입력 → 처리 → LLM 판단 → 저장의 흐름을 거쳐, 대시보드와 MCP Server 두 갈래로 결과를 제공합니다.

```
┌──────────────────────────────────────────────────────────┐
│                 Input Layer (문서 입력)                     │
│  Confluence MCP · 파일 업로드 ·                              │
│  직접 MD 작성 · Git 코드 수집                                │
└───────────────────────────┬────────────────────────────────┘
                            ▼
                  ┌───────────────────┐
                  │  Processor Layer   │  파싱 · 정규화
                  └─────────┬─────────┘
                            ▼
                  ┌───────────────────┐
                  │   LLM Classifier   │  청크 vs 그래프 판단
                  └─────────┬─────────┘     (Git 코드는 AST 정적분석)
                            ▼
                  ┌───────────────────┐
                  │   Storage Layer    │  벡터DB + 그래프DB + SQLite
                  └─────────┬─────────┘
              ┌─────────────┴─────────────┐
              ▼                           ▼
   ┌──────────────────┐        ┌──────────────────────┐
   │  Dashboard (Web)  │        │   MCP Server (stdio)  │
   │  원본 뷰어 +       │        │   컨텍스트 검색/조립    │
   │  데이터 시각화      │        │   ← 사내 LLM 앱 연동    │
   └──────────────────┘        └──────────────────────┘
```

---

## 문서 입력 방식

환경과 콘텐츠 유형에 맞춰 네 가지 입력 경로를 제공합니다.

1. **Confluence MCP Client** — 사내 Confluence MCP 서버에 연결해 키워드 검색·트리 탐색·내 문서로 임포트
2. **파일 업로드** — 드래그 앤 드롭으로 `.md` · `.txt` · `.html` 업로드 (원본 보관 + LLM 처리)
3. **직접 마크다운 작성** — 실시간 미리보기 에디터로 작성, 저장 즉시 처리 파이프라인으로 전달
4. **Git 코드 수집 `Phase 9`** — 레포를 clone/pull하여 **AST 기반 정적 추출**(LLM 호출 없음)로 함수·클래스·메서드·import를 정확히 추출. 메서드 단위로 청킹하고 import/contains 관계를 그래프로 저장. 코드는 이미 구조화된 데이터이므로 AST 분석으로 100% 정확·고속·비용 제로 달성 (`D-036`, `D-037`)

---

## LLM 저장 방식 판단

문서가 입력되면 LLM이 구조·엔티티·관계를 분석하여 최적 저장 방식을 결정합니다.
(Git 코드는 AST 처리로 Classifier를 건너뜁니다.)

| 저장 방식 | 적합한 문서 유형 | 예시 |
|-----------|----------------|------|
| **텍스트 청크** (벡터DB) | 서술형 문서, 가이드, 매뉴얼, 긴 설명 | 온보딩 가이드, API 문서, 회의록 |
| **그래프 DB** | 엔티티 간 관계가 중요한 문서 | 아키텍처 문서, 팀 구성, 의존성 맵 |
| **혼합** | 서술 + 관계 정보가 공존 | 프로젝트 기획서(설명 + 마일스톤 관계) |

---

## 변경 감지 & 재처리

원본이 바뀌면 파생 데이터(청크·그래프)도 자동 갱신됩니다. 전략은 **Delete & Recreate**.

1. **기존 데이터 정리** — 벡터DB 청크, 그래프DB 노드/엣지, SQLite 레코드를 문서 소유권 기준으로 삭제
2. **원본 갱신** — `original_content` 업데이트, `content_hash` 갱신, 상태를 `processing`으로 전환
3. **재처리 파이프라인 실행** — LLM Classifier 재판정(저장 방식이 바뀔 수 있음) → 청킹/임베딩 또는 그래프 추출 → 저장 → `completed`

---

## MCP Server

이 시스템은 **MCP(Model Context Protocol) Server**로 동작합니다.
사내 LLM 앱이 질의하면 관련 컨텍스트를 검색·조립해 응답합니다.

### 제공 MCP Tools

| Tool | 설명 | 주요 파라미터 |
|------|------|--------------|
| `search_context` | 질의로 관련 사내 지식 컨텍스트를 검색·조립 | `query`, `max_chunks?`, `include_graph?` |
| `list_documents` | 등록된 문서 목록 조회 | `source_type?`, `status?` |
| `get_document` | 문서의 원본/처리 데이터 조회 | `document_id`, `format` |
| `get_graph_context` | 엔티티 중심 그래프 관계 탐색 | `entity_name`, `depth?` |

### 서버 실행

```bash
# stdio 모드 (기본)
context-loop mcp serve

# SSE 모드 (원격 접근)
context-loop mcp serve --transport sse --port 3001
```

### 클라이언트 설정 예시 (Claude Code)

```json
{
  "mcpServers": {
    "context-loop": {
      "command": "context-loop",
      "args": ["mcp", "serve"],
      "env": {}
    }
  }
}
```

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 웹 프레임워크 | FastAPI + Jinja2 |
| 프론트엔드 | HTMX + Alpine.js (Pico CSS) |
| 그래프 시각화 | vis.js / D3.js |
| 벡터 DB | ChromaDB (로컬 임베디드) |
| 그래프 DB | NetworkX + SQLite |
| 메타데이터 DB | SQLite |
| 임베딩 / LLM | 자체 엔드포인트(OpenAI 호환) / OpenAI / Claude |
| MCP SDK | mcp (Python) + FastMCP — stdio/SSE |
| 인증 저장 | keyring (OS 네이티브 키체인) |

**Python ≥ 3.11** · FastAPI · ChromaDB · NetworkX · MCP · 로컬 퍼스트

---

<sub>Project Context Loop System · 사내 지식을 LLM 컨텍스트로 · 프로젝트 소개 문서</sub>
