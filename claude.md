# Project Context Loop System

## 프로젝트 개요

사내 지식(Confluence 문서 등)을 LLM context로 변환·저장하고, 이를 시각적으로 관리할 수 있는 **웹 대시보드** 시스템이다.
사용자는 대시보드를 통해 Confluence 문서를 임포트하거나, 파일을 업로드하거나, 직접 마크다운을 작성하여 사내 지식을 등록할 수 있다.
등록된 문서는 LLM이 내용을 분석하여 **텍스트 청크** 또는 **그래프 DB** 중 최적의 저장 방식을 자동 결정한 뒤 저장한다.
대시보드에서 원본 문서와 변환된 데이터를 시각적으로 확인·탐색할 수 있다.
또한 **MCP(Model Context Protocol) Server**로 동작하여, 사내 LLM 애플리케이션이 질의하면 저장된 지식에서 적절한 컨텍스트를 검색·조립하여 응답한다.

## 시스템 아키텍처

```
┌──────────────────────────────────────────────┐
│              Input Layer (문서 입력)           │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌──────────────┐ │
│  │Confluence │  │파일 업로드│  │직접 MD 작성│  │Confluence    │ │
│  │ API 임포트│  │(.md 등)  │  │(에디터)    │  │MCP Client    │ │
│  └─────┬────┘  └─────┬────┘  └─────┬──────┘  └──────┬───────┘ │
└────────┼─────────────┼─────────────┼────────────────┼──────────┘
         └─────────────┼─────────────┘
                       ▼
              ┌─────────────────┐
              │  Processor Layer │  ← 문서 파싱, 정규화
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  LLM Classifier  │  ← 저장 방식 판단 (청크 vs 그래프)
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  Storage Layer   │  ← 벡터DB (청크) + 그래프DB + SQLite (메타)
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
┌─────────────────┐   ┌─────────────────────┐
│  Dashboard (Web) │   │  MCP Server (stdio) │
│  원본 뷰어 +     │   │  사내 LLM 앱 연동    │
│  데이터 시각화    │   │  컨텍스트 검색/조립   │
└─────────────────┘   └─────────────────────┘
                              ▲
                              │ MCP Protocol (JSON-RPC over stdio)
                              │
                      ┌───────┴───────┐
                      │ 사내 LLM 앱    │
                      │ (Claude Code,  │
                      │  커스텀 에이전트│
                      │  등)           │
                      └───────────────┘
```

## 핵심 설계 원칙

1. **대시보드 중심**: 모든 조작(입력, 조회, 탐색)은 웹 대시보드에서 수행.
2. **다중 입력**: Confluence API 임포트, Confluence MCP Client, 파일 업로드, 직접 MD 작성 등 다양한 입력 경로 지원.
3. **LLM 기반 저장 판단**: 문서 내용을 LLM이 분석하여 텍스트 청크 / 그래프 DB 중 최적 저장 방식을 자동 결정.
4. **시각적 데이터 탐색**: 원본 문서와 변환된 데이터(청크, 그래프 노드/엣지)를 대시보드에서 시각적으로 확인 가능.
5. **MCP Server 제공**: 사내 LLM 애플리케이션이 MCP 프로토콜로 질의하면 벡터 검색 + 그래프 탐색으로 적절한 컨텍스트를 조립하여 응답.
6. **로컬 퍼스트**: 모든 데이터는 사용자 PC에 저장. 외부 서버 의존 최소화.
7. **보안 우선**: 인증 토큰은 OS 키체인(keyring)에 저장. 설정 파일에 시크릿 노출 금지.

## 문서 입력 방식

### 1. Confluence API 임포트

Confluence REST API를 통해 스페이스/페이지를 선택적으로 가져온다.

- 대시보드에서 Confluence 연결 설정 (URL, 인증 정보)
- 스페이스/페이지 트리를 탐색하여 임포트 대상 선택
- HTML → 마크다운 변환 후 저장
- 이후 변경분 증분 동기화 지원

### 2. Confluence MCP Client 임포트

사내 Confluence MCP Server에 MCP Client로 연결하여 문서를 가져온다. (D-016)

- REST API 접근이 차단된 환경에서 Confluence 연동 대안
- MCP 프로토콜(SSE/stdio)로 사내 MCP 서버에 연결
- 3가지 임포트 방법: 키워드 검색 (`searchContent`), 스페이스 트리 탐색 (`getSpaceInfoAll` + `getChild`), 내가 작성한 문서 (`getUserContributedPages`)
- 선택한 페이지의 본문을 `getPage`로 가져와 저장
- 인증/권한은 MCP 서버 측에서 처리

### 3. 파일 업로드

마크다운(.md) 파일 등을 대시보드에서 직접 업로드한다.

- 드래그 앤 드롭 또는 파일 선택으로 업로드
- 지원 포맷: `.md`, `.txt`, `.html` (추후 `.pdf`, `.docx` 확장 가능)
- 업로드된 파일은 원본 그대로 보관 + LLM 처리

### 4. 직접 마크다운 작성

대시보드 내장 에디터에서 직접 마크다운 문서를 작성한다.

- 마크다운 에디터 (실시간 미리보기 지원)
- 작성 완료 시 즉시 LLM 처리 파이프라인으로 전달
- 기존 문서 수정/업데이트 가능

### 5. Git 코드 수집 (Phase 9)

Git 레포지토리에서 소스 코드를 수집하여 사내 컨텍스트로 변환한다.

- config.yaml에 레포지토리 URL, 브랜치, 상품별 스코프 정의
- `git clone/pull`로 레포 동기화, `content_hash` 기반 변경 감지
- **AST 기반 정적 추출** (LLM 호출 없음, D-036):
  - Python: `ast` 모듈로 함수/클래스/메서드/import 정확 추출
  - Go, Java, TypeScript, JavaScript: 키워드 + 중괄호 매칭
  - 기타 언어: 파일 전체를 단일 심볼로 반환
- **메서드 단위 청킹** (D-037): 클래스 내부 메서드를 개별 청크로 분할, `section_path`에 `file > class > method` 계층 구조 기록
- **임베딩/저장 분리**: 임베딩 텍스트(이름+시그니처+docstring)로 검색 정확도 확보, 저장 문서(전체 코드)로 원본 반환
- **import 그래프**: 파일 간 import 관계 + 클래스→메서드 contains 관계를 GraphStore에 저장

```
Git Repository
   │
   ▼
git clone/pull → 파일 수집 → store git_code (원본 코드 DB 저장)
   │
   ▼
AST 정적 분석 (extract_code_symbols)
   │
   ├─ to_chunks() → 심볼 단위 청크 → 임베딩 → 벡터DB 저장
   │
   └─ to_graph_data() → import/contains 관계 → GraphStore 저장
```

> **설계 배경**: 초기에는 LLM 기반 그래프 추출(graph_extractor.py)을 사용했으나, 코드는 토큰 밀도가 높아 빈번한 타임아웃이 발생했다. 코드는 이미 구조화된 데이터이므로 LLM "추론"이 불필요 — AST 정적 분석으로 100% 정확하고, 파일당 수 ms로 처리하며, 비용 제로이다. (D-036)

## LLM 저장 방식 판단

문서가 입력되면 LLM이 내용을 분석하여 최적 저장 방식을 결정한다. (Git 코드는 AST 기반으로 처리하므로 LLM Classifier를 건너뛴다.)

### 판단 기준

| 저장 방식 | 적합한 문서 유형 | 예시 |
|-----------|----------------|------|
| **텍스트 청크** (벡터DB) | 서술형 문서, 가이드, 매뉴얼, 긴 설명 | 온보딩 가이드, API 문서, 회의록 |
| **그래프 DB** | 엔티티 간 관계가 중요한 문서, 조직도, 시스템 구조 | 아키텍처 문서, 팀 구성, 의존성 맵 |
| **혼합** | 서술 + 관계 정보가 공존하는 문서 | 프로젝트 기획서 (설명 + 마일스톤 관계) |

### 처리 플로우

```
문서 입력
   │
   ▼
LLM 분석 (문서 구조, 엔티티, 관계 존재 여부 판단)
   │
   ├─ "chunk" 판정 → 텍스트 청킹 → 임베딩 → 벡터DB 저장
   │
   ├─ "graph" 판정 → 엔티티/관계 추출 → 그래프DB 저장
   │
   └─ "hybrid" 판정 → 청크 + 그래프 모두 저장
```

## 문서 변경 감지 및 재처리

문서는 지속적으로 변경될 수 있으므로, 저장된 컨텍스트 데이터(청크, 그래프)도 원본 변경에 맞춰 자동으로 갱신되어야 한다.

### 변경 감지 방식

| 입력 소스 | 감지 방법 |
|-----------|----------|
| **Confluence** | 증분 동기화 시 `version` 비교 + `content_hash` 대조 |
| **파일 업로드** | 동일 파일 재업로드 시 `content_hash` 비교 |
| **직접 작성** | 에디터에서 저장 시 `content_hash` 비교 |

### 재처리 전략: Delete & Recreate

문서 변경이 감지되면 해당 문서의 기존 파생 데이터를 모두 삭제한 뒤 새로 생성한다.

```
변경 감지 (content_hash 불일치)
   │
   ▼
┌─────────────────────────────────┐
│  1. 기존 데이터 정리             │
│     - 벡터DB에서 해당 문서 청크 삭제 │
│     - 그래프DB에서 해당 문서       │
│       소유 노드/엣지 삭제         │
│     - SQLite chunks, graph_nodes, │
│       graph_edges 레코드 삭제     │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  2. 원본 갱신                    │
│     - documents.original_content │
│       업데이트                    │
│     - content_hash 갱신          │
│     - status → "processing"      │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  3. 재처리 파이프라인 실행        │
│     - LLM Classifier 재판정      │
│       (저장 방식이 바뀔 수 있음)  │
│     - 청킹/임베딩 또는            │
│       그래프 추출 재실행          │
│     - 새 데이터 저장              │
│     - status → "completed"       │
└─────────────────────────────────┘
```

### 그래프 엔티티 관리

여러 문서에서 동일 엔티티(예: "인증 서비스")를 참조할 수 있다. 이를 안전하게 관리하기 위해:

- **문서 소유권 기반 삭제**: `graph_nodes`와 `graph_edges`에 `document_id`가 있으므로, 해당 문서 소유 레코드만 삭제한다.
- **엔티티 병합 테이블**: 동일 엔티티가 여러 문서에서 등장하면 `entity_name` + `entity_type`으로 논리적 병합을 관리한다. 한 문서의 노드가 삭제되어도 다른 문서의 동일 엔티티는 유지된다.
- **고아 엣지 정리**: 노드 삭제 후 연결된 엣지 중 양쪽 노드가 모두 없는 경우 자동 정리한다.

### 재처리 이력 추적

모든 재처리는 `processing_history` 테이블에 기록된다.

- `action`: "created" / "updated" / "reprocessed"
- 이전 `storage_method`와 새 `storage_method`가 다를 수 있음 (예: 문서 구조가 바뀌어 chunk → hybrid로 변경)
- 대시보드 문서 상세 뷰의 **메타데이터 탭**에서 처리 이력 타임라인 확인 가능

### Confluence 자동 동기화

Confluence 소스 문서는 주기적 증분 동기화로 자동 갱신된다.

- 설정된 주기(`sync_interval_minutes`)마다 변경된 페이지를 감지
- 변경된 문서만 선별하여 재처리 파이프라인 실행
- 대시보드에서 수동 동기화 트리거 가능 ("지금 동기화" 버튼)
- Confluence에서 삭제된 페이지는 로컬 데이터도 함께 정리 (옵션)

## 대시보드 화면 구성

### 메인 대시보드

- 등록된 문서 목록 (소스별 필터링: Confluence / 업로드 / 직접 작성)
- 문서별 상태 표시 (원본, 처리 중, 처리 완료, 변경 감지됨)
- 저장 방식 태그 (chunk / graph / hybrid)
- 전체 통계 (문서 수, 청크 수, 그래프 노드/엣지 수)
- Confluence 동기화 상태 및 "지금 동기화" 버튼

### 문서 상세 뷰

- **원본 탭**: 원본 마크다운/HTML 렌더링
- **청크 탭**: 분할된 텍스트 청크 목록 (하이라이트로 원본 내 위치 표시)
- **그래프 탭**: 추출된 엔티티/관계를 인터랙티브 그래프로 시각화
- **메타데이터 탭**: 소스 정보, 처리 이력 타임라인, 버전 변경 내역

### Confluence 임포트 화면

- Confluence 연결 설정
- 스페이스/페이지 트리 브라우저
- 선택적 임포트 + 동기화 설정

### 마크다운 에디터

- 분할 뷰 (에디터 | 미리보기)
- 새 문서 작성 / 기존 문서 수정

## MCP Server

### 개요

이 시스템은 MCP(Model Context Protocol) Server로도 동작한다. 사내 LLM 애플리케이션(Claude Code, 커스텀 에이전트 등)이 MCP 클라이언트로서 이 서버에 질의하면, 저장된 사내 지식에서 관련 컨텍스트를 검색·조립하여 응답한다.

### MCP 전송 방식

- **stdio**: 기본 전송 방식. LLM 앱이 이 서버를 subprocess로 실행하여 stdin/stdout으로 JSON-RPC 통신.
- **SSE (Server-Sent Events)**: 원격 접근이 필요한 경우 HTTP 기반 SSE 전송 지원 (선택적).

### 제공 MCP Tools

| Tool 이름 | 설명 | 파라미터 |
|-----------|------|---------|
| `search_context` | 질의 문자열로 관련 사내 지식 컨텍스트를 검색·조립하여 반환 | `query` (str), `max_chunks` (int, optional), `include_graph` (bool, optional) |
| `list_documents` | 등록된 문서 목록 조회 | `source_type` (str, optional), `status` (str, optional) |
| `get_document` | 특정 문서의 원본 또는 처리된 데이터 조회 | `document_id` (int), `format` ("original" \| "chunks" \| "graph") |
| `get_graph_context` | 특정 엔티티 중심으로 그래프 관계를 탐색하여 컨텍스트 반환 | `entity_name` (str), `depth` (int, optional) |

### 컨텍스트 조립 플로우

```
LLM 앱 질의 (MCP Tool Call)
   │
   ▼
┌─────────────────────────────┐
│  Query Processor             │
│  - 질의 임베딩 생성          │
│  - 벡터DB 유사도 검색        │
│  - 그래프DB 관련 엔티티 탐색  │
└──────────┬──────────────────┘
           ▼
┌─────────────────────────────┐
│  Context Assembler           │
│  - 청크 + 그래프 결과 병합   │
│  - 중복 제거, 관련도 정렬    │
│  - 출처 메타데이터 첨부      │
└──────────┬──────────────────┘
           ▼
   MCP Tool Response
   (컨텍스트 텍스트 + 출처 정보)
```

### MCP 서버 실행 방법

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

## 프로젝트 구조

```
context-loop/
├── pyproject.toml
├── README.md
├── src/
│   └── context_loop/
│       ├── __init__.py
│       ├── config.py               # 설정 로드/저장
│       ├── auth.py                 # 토큰 관리 (keyring 연동)
│       ├── ingestion/              # 문서 입력
│       │   ├── __init__.py
│       │   ├── confluence.py       # Confluence API 임포트
│       │   ├── uploader.py         # 파일 업로드 처리
│       │   └── editor.py           # 직접 작성 문서 처리
│       ├── processor/              # 문서 처리
│       │   ├── __init__.py
│       │   ├── parser.py           # HTML/MD 파싱, 정규화
│       │   ├── classifier.py       # LLM 저장 방식 판단
│       │   ├── chunker.py          # 텍스트 청킹
│       │   ├── graph_extractor.py  # 엔티티/관계 추출 (문서용 LLM 기반)
│       │   ├── ast_code_extractor.py # 코드 심볼/import 추출 (AST 정적 분석, D-036)
│       │   └── embedder.py         # 임베딩 생성
│       ├── storage/                # 저장소
│       │   ├── __init__.py
│       │   ├── vector_store.py     # ChromaDB 래퍼 (텍스트 청크)
│       │   ├── graph_store.py      # 그래프DB 래퍼 (엔티티/관계)
│       │   └── metadata_store.py   # SQLite 메타데이터
│       ├── sync/                   # Confluence 동기화
│       │   ├── __init__.py
│       │   └── engine.py           # 증분 동기화 로직
│       ├── mcp/                    # MCP Server
│       │   ├── __init__.py
│       │   ├── server.py           # MCP 서버 메인 (FastMCP 기반)
│       │   ├── tools.py            # MCP Tool 정의 (search_context 등)
│       │   └── context_assembler.py # 컨텍스트 검색·조립 로직
│       └── web/                    # 웹 대시보드
│           ├── __init__.py
│           ├── app.py              # FastAPI/Streamlit 앱
│           ├── api/                # REST API 엔드포인트
│           │   ├── __init__.py
│           │   ├── documents.py    # 문서 CRUD
│           │   ├── confluence.py   # Confluence 연동 API
│           │   └── query.py        # 질의 API
│           └── frontend/           # 프론트엔드 정적 파일
│               ├── dashboard.html
│               ├── editor.html
│               └── viewer.html
├── tests/
│   ├── test_ingestion/
│   ├── test_processor/
│   ├── test_storage/
│   ├── test_mcp/
│   └── test_web/
└── config/
    └── default.yaml                # 기본 설정 템플릿
```

## 기술 스택

| 영역 | 기술 | 선택 이유 |
|------|------|-----------|
| 웹 프레임워크 | FastAPI + Jinja2 또는 Streamlit | API + 대시보드 통합 |
| 프론트엔드 | HTMX + Alpine.js 또는 Streamlit | 경량 인터랙티브 UI |
| 그래프 시각화 | vis.js 또는 D3.js | 인터랙티브 그래프 렌더링 |
| 벡터 DB | ChromaDB | 로컬 임베디드 모드, pip install만으로 사용 |
| 그래프 DB | NetworkX + SQLite 또는 Neo4j Embedded | 엔티티/관계 저장, 로컬 실행 |
| 메타데이터 DB | SQLite | 파일 기반, 별도 서버 불필요 |
| 임베딩 | 자체 엔드포인트(OpenAI 호환) / OpenAI / 로컬 모델 | 자체 모델 서버 우선, OpenAI 호환 API 지원 |
| LLM | 자체 엔드포인트(OpenAI 호환) / OpenAI GPT-4o / Claude | 자체 모델 서버 우선, OpenAI 호환 API 지원 |
| MCP SDK | mcp (Python SDK) + FastMCP | MCP 프로토콜 서버 구현, stdio/SSE 전송 지원 |
| 인증 저장 | keyring | OS 네이티브 키체인 연동 |
| HTTP 클라이언트 | httpx | async 지원, Confluence API 호출용 |
| 마크다운 에디터 | EasyMDE 또는 Toast UI Editor | 브라우저 기반 MD 편집 |
| 패키징 | pyproject.toml + hatchling | 모던 Python 패키징 표준 |

## 설정 파일 구조

```yaml
# ~/.context-loop/config.yaml

app:
  data_dir: "~/.context-loop/data"        # 로컬 DB 저장 경로
  log_level: "INFO"

sources:
  confluence:
    enabled: true
    base_url: "https://yourcompany.atlassian.net"
    email: "user@company.com"
    token_storage: "keyring"              # 토큰은 OS 키체인에 저장
    sync_interval_minutes: 30             # 증분 동기화 주기

processor:
  chunk_size: 512                         # 청크 토큰 수
  chunk_overlap: 50                       # 청크 간 겹침
  embedding_model: "text-embedding-3-small"
  embedding_provider: "endpoint"          # "openai" | "local" | "endpoint"
  embedding_endpoint: ""                  # embedding_provider: "endpoint"일 때 사용 (예: "http://localhost:8080/v1")
  embedding_api_key: ""                   # 엔드포인트 인증 키 (불필요한 경우 빈 문자열)

llm:
  provider: "endpoint"                    # "openai" | "anthropic" | "endpoint"
  model: ""                               # 사용할 모델 ID
  endpoint: ""                            # provider: "endpoint"일 때 사용 (예: "http://localhost:11434/v1")
  api_key: ""                             # 엔드포인트 인증 키 (불필요한 경우 빈 문자열)
  api_key_storage: "keyring"              # provider: "openai" | "anthropic"일 때 keyring 사용
  classifier_prompt: "default"            # 저장 방식 판단 프롬프트

storage:
  vector_db: "chromadb"
  graph_db: "networkx"                    # "networkx" | "neo4j"

web:
  host: "127.0.0.1"
  port: 8000

mcp:
  transport: "stdio"                      # "stdio" | "sse"
  sse_port: 3001                          # SSE 모드 시 포트
  max_context_chunks: 10                  # 응답에 포함할 최대 청크 수
  include_graph_by_default: true          # 그래프 컨텍스트 기본 포함 여부
  context_max_tokens: 4096                # 조립된 컨텍스트 최대 토큰 수
```

## Confluence 커넥터 상세

### 인증 방식

- **Confluence Cloud**: Basic Auth (`email:api_token`을 base64 인코딩)
- **Confluence Data Center**: Bearer Token (Personal Access Token)

```python
# Cloud
headers = {
    "Authorization": f"Basic {base64.b64encode(f'{email}:{token}'.encode()).decode()}"
}

# Data Center
headers = {
    "Authorization": f"Bearer {pat_token}"
}
```

### 주요 API 엔드포인트

| 용도 | 엔드포인트 | 비고 |
|------|-----------|------|
| 스페이스 목록 | `GET /wiki/api/v2/spaces` | 대시보드에서 선택용 |
| 페이지 목록 | `GET /wiki/api/v2/spaces/{id}/pages` | 페이지네이션 필수 |
| 페이지 상세 | `GET /wiki/api/v2/pages/{id}?body-format=storage` | 본문 포함 |
| 변경 감지 | `GET /wiki/rest/api/content?expand=version&orderby=lastmodified` | 증분 동기화용 |

## 메타데이터 DB 스키마 (SQLite)

```sql
-- 등록된 문서
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,            -- "confluence", "upload", "manual"
    source_id TEXT,                       -- Confluence 페이지 ID (업로드/작성은 NULL)
    title TEXT NOT NULL,
    original_content TEXT,                -- 원본 텍스트 (마크다운)
    content_hash TEXT,                    -- 변경 감지용 해시
    storage_method TEXT,                  -- "chunk", "graph", "hybrid"
    status TEXT DEFAULT 'pending',        -- "pending", "processing", "completed", "failed", "changed"
    version INTEGER DEFAULT 1,            -- 처리 버전 (재처리 시 증가)
    url TEXT,                             -- 원본 링크 (Confluence)
    author TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_type, source_id)
);

-- 텍스트 청크 (벡터DB 매핑)
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,                  -- 벡터DB의 chunk ID
    document_id INTEGER REFERENCES documents(id),
    chunk_index INTEGER,
    content TEXT,                         -- 청크 텍스트 (대시보드 표시용)
    token_count INTEGER
);

-- 그래프 노드
CREATE TABLE graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id),
    entity_name TEXT NOT NULL,
    entity_type TEXT,                     -- "person", "system", "concept", "team" 등
    properties TEXT                       -- JSON
);

-- 그래프 엣지
CREATE TABLE graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id),
    source_node_id INTEGER REFERENCES graph_nodes(id),
    target_node_id INTEGER REFERENCES graph_nodes(id),
    relation_type TEXT,                   -- "belongs_to", "depends_on", "manages" 등
    properties TEXT                       -- JSON
);

-- 처리 이력
CREATE TABLE processing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id),
    action TEXT,                           -- "created", "updated", "reprocessed"
    prev_storage_method TEXT,              -- 이전 저장 방식 (재처리 시)
    new_storage_method TEXT,               -- 새 저장 방식
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,
    error_message TEXT
);
```

## 구현 순서

아래 순서로 점진적으로 구현한다. 각 단계가 독립적으로 동작 가능해야 한다.

### Phase 1: 기반 구조
1. 프로젝트 스캐폴딩 (pyproject.toml, 디렉토리 구조)
2. 설정 관리 (config.yaml 로드/저장, 기본값)
3. 인증 모듈 (keyring 연동, 토큰 저장/조회)
4. SQLite 메타데이터 저장소 세팅

### Phase 2: 문서 입력 파이프라인
5. 파일 업로드 처리 (MD/TXT/HTML → 원본 저장)
6. 마크다운 직접 작성 저장
7. Confluence API 임포트 (인증, 스페이스/페이지 조회, HTML→MD 변환)
8. Confluence 증분 동기화

### Phase 3: LLM 저장 방식 판단 + 처리
9. LLM Classifier 구현 (문서 분석 → chunk/graph/hybrid 판정)
10. 텍스트 청킹 모듈 (토큰 기반 분할)
11. 임베딩 + ChromaDB 벡터 저장
12. 그래프 엔티티/관계 추출 모듈
13. 그래프DB 저장 (NetworkX + SQLite)

### Phase 4: 웹 대시보드
14. 기본 대시보드 레이아웃 (문서 목록, 통계)
15. 문서 상세 뷰 (원본 탭, 청크 탭, 메타데이터 탭)
16. 그래프 시각화 탭 (인터랙티브 그래프 렌더링)
17. Confluence 임포트 UI (연결 설정, 스페이스 브라우저)
18. 마크다운 에디터 통합
19. 파일 업로드 UI

### Phase 5: MCP Server
20. MCP 서버 기본 구조 (FastMCP, stdio 전송)
21. `search_context` Tool 구현 (벡터 검색 + 그래프 탐색 → 컨텍스트 조립)
22. `list_documents`, `get_document`, `get_graph_context` Tool 구현
23. SSE 전송 지원 (선택적 원격 접근)
24. MCP 클라이언트 연동 테스트 (Claude Code 등)

### Phase 6: 질의 및 고도화
25. 대시보드 내 채팅 인터페이스 (RAG 파이프라인 활용)
26. 출처 표시 (원본 문서 링크)

### Phase 7: 배포
27. 패키징 및 사내 배포
28. 초기 설정 마법사 (대시보드 내)

## 코딩 컨벤션

- Python 3.11+
- 타입 힌트 필수 (strict mypy 호환)
- async/await 기반 I/O
- docstring: Google 스타일
- 테스트: pytest + pytest-asyncio
- 포매터: ruff
- 린터: ruff
- 커밋 메시지: Conventional Commits (feat:, fix:, docs:, refactor:, test:)

---

## 로컬 환경 설정 및 실행

자세한 설치·실행 방법은 **[docs/setup.md](docs/setup.md)** 를 참조한다.

### 빠른 시작 요약

```bash
# 1. 가상환경 생성 및 패키지 설치
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. 설정 파일 초기화
mkdir -p ~/.context-loop && cp config/default.yaml ~/.context-loop/config.yaml

# 3. 웹 대시보드 실행 (--factory 플래그 필수)
python3 -m uvicorn "context_loop.web.app:create_app" --factory --host 127.0.0.1 --port 8000 --reload
```

> `context_loop.web.app:app` 형태로 실행하면 에러가 발생한다. 반드시 `create_app` + `--factory` 사용.

---

## 세션 간 Context 관리

### 목적

이 프로젝트는 여러 세션에 걸쳐 점진적으로 구현된다. 새 세션이 시작될 때 이전 맥락을 빠르게 파악하기 위해 `.context/` 폴더를 운영한다.

### 폴더 구조

```
.context/
├── progress/
│   └── STATUS.md           # 구현 진행 상황 (현재 Phase/Step, 체크리스트)
├── decisions/
│   └── DECISIONS.md         # 설계 결정 기록 (번호순, 변경 이력 보존)
├── sessions/
│   └── {날짜}_{주제}.md     # 세션별 작업 로그
└── ISSUES.md                # 미해결 이슈, TODO, 개선 아이디어
```

### 각 파일의 역할

| 파일 | 역할 | 갱신 시점 |
|------|------|-----------|
| `progress/STATUS.md` | 현재 Phase/Step, 전체 체크리스트 | 매 구현 단위 완료 시 |
| `decisions/DECISIONS.md` | 설계 결정 사항과 근거 (번호제) | 새 결정이 내려질 때마다 |
| `sessions/{날짜}_{주제}.md` | 세션별 수행 내용, 생성 파일, 다음 TODO | 매 세션 종료 시 |
| `ISSUES.md` | 이슈, 블로커, 개선 아이디어 | 발견 즉시 |

### 새 세션 시작 시 규칙

새 세션에서 이 프로젝트 작업을 재개할 때는 반드시 아래 순서로 context를 로드한다:

1. **`claude.md`** 읽기 — 프로젝트 전체 설계 파악
2. **`.context/progress/STATUS.md`** 읽기 — 현재 어디까지 진행됐는지 확인
3. **`.context/ISSUES.md`** 읽기 — 미해결 이슈 확인
4. **`.context/decisions/DECISIONS.md`** 읽기 — 이전 결정 사항 파악
5. 필요 시 최근 세션 로그 (`sessions/` 내 가장 최근 파일) 참조

### 세션 종료 시 규칙

작업을 마칠 때는 반드시 아래를 수행한다:

1. **`progress/STATUS.md`** 갱신 — 완료된 항목 체크, 현재 Phase/Step 업데이트
2. **세션 로그 작성** — `sessions/{날짜}_{주제}.md` 파일에 수행 내용, 생성 파일, 다음 TODO 기록
3. **`ISSUES.md`** 갱신 — 새로 발견된 이슈 추가, 해결된 이슈 이동
4. 새 결정이 있었다면 **`decisions/DECISIONS.md`**에 추가

### 결정 기록 형식

```markdown
## D-{번호}: {제목}

- **일시**: YYYY-MM-DD
- **맥락**: 어떤 상황에서 이 결정이 필요했는지
- **결정**: 무엇을 선택했는지
- **이유**: 왜 이 선택을 했는지
```
