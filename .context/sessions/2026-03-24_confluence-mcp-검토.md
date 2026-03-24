# 2026-03-24 세션: Confluence MCP Server 연동 방안 검토

## 배경

- Confluence REST API를 통한 사내 문서 임포트가 API 접근 제한으로 불가능한 상황
- 차선책으로 사내에 이미 운영 중인 **Confluence MCP Server**를 활용하여 문서를 가져오는 방안 검토
- 현재 시스템은 `mcp>=1.0.0` 라이브러리를 이미 사용 중 (MCP Server 역할), 이를 **MCP Client** 역할로도 확장

## Confluence MCP Server 제공 도구

사내 Confluence MCP Server가 제공하는 도구 9종:

| 도구 | 용도 | 임포트 활용 |
|------|------|------------|
| `getSpaceInfoAll` | 전체 스페이스 목록 조회 | 탐색 진입점 |
| `getSpaceInfo` | 특정 스페이스 상세 정보 | 스페이스 상세 |
| `searchContent` | 콘텐츠 키워드 검색 | 키워드 기반 탐색 |
| `getPage` | 페이지 단건 조회 (본문 포함) | **본문 가져오기 (핵심)** |
| `getChild` | 하위 페이지 목록 조회 | 트리 탐색 |
| `getUserContributedPages` | 사용자 기여 페이지 조회 | 내가 쓴 문서 탐색 |
| `createContent` | 콘텐츠 생성 | 임포트와 무관 |
| `updateContent` | 콘텐츠 수정 | 임포트와 무관 |
| `createComment` | 댓글 생성 | 임포트와 무관 |

임포트에 활용 가능한 도구: `getSpaceInfoAll`, `getSpaceInfo`, `searchContent`, `getPage`, `getChild`, `getUserContributedPages` (6종)

## 사용자 시나리오 검토

### 시나리오 1: 검색 기반 임포트 (1순위 추천)

```
사용자 → 검색어 입력 → searchContent → 결과 목록에서 선택 → getPage × N → MetadataStore 저장
```

- 가장 범용적이고 구현이 단순
- `searchContent` 1회 + `getPage` N회로 완료

### 시나리오 2: 스페이스 트리 탐색 임포트 (2순위)

```
사용자 → getSpaceInfoAll → 스페이스 선택 → getChild(root) → 트리 펼침 → getPage × N
```

- 파일 탐색기처럼 문서 구조를 눈으로 확인하며 선택
- `getChild` 재귀 호출로 트리 탐색

### 시나리오 3: 내가 작성한 문서 임포트 (3순위)

```
사용자 → 사용자 ID 입력 → getUserContributedPages → 목록에서 선택 → getPage × N
```

- 본인 업무 관련 문서를 빠르게 모을 수 있음

### 통합 UI 제안

세 시나리오를 **탭 기반 UI**로 통합:

```
┌──────────┐ ┌──────────┐ ┌─────────────────┐
│ 🔍 검색  │ │ 📁 탐색  │ │ 👤 내 문서      │
└──────────┘ └──────────┘ └─────────────────┘
```

공통: 선택한 페이지에 대해 "하위 페이지 포함" 옵션 → `getChild` 활용

## 기술적 구현 방안

### 신규 모듈: `src/context_loop/ingestion/mcp_confluence.py`

MCP 클라이언트로 사내 Confluence MCP Server에 연결하여 문서를 가져오는 모듈:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

class ConfluenceMCPClient:
    """사내 Confluence MCP Server 클라이언트"""
    async def connect(server_url_or_command) -> ClientSession
    async def list_tools() -> list
    async def search_content(query) -> list[dict]
    async def get_page(page_id) -> dict
    async def get_child_pages(page_id) -> list[dict]
    async def get_all_spaces() -> list[dict]
    async def get_user_contributed_pages(user_id) -> list[dict]

async def import_page_via_mcp(session, store, page_id) -> dict:
    # 기존 import_page()와 동일한 반환 형식
    # {"id": int, "source_type": "confluence_mcp", "created": bool, "changed": bool}
```

### 설정 추가 (`config/default.yaml`)

```yaml
sources:
  confluence_mcp:
    enabled: false
    transport: "sse"                              # "sse" 또는 "stdio"
    server_url: "http://internal-mcp:3001/sse"    # SSE 전송 시
    # command: "confluence-mcp-server"             # stdio 전송 시
    # args: []
    sync_interval_minutes: 30
```

### Web API 엔드포인트

```
POST /api/confluence-mcp/connect          # MCP 서버 연결 테스트
GET  /api/confluence-mcp/tools            # 사용 가능한 도구 목록
GET  /api/confluence-mcp/spaces           # 스페이스 목록 (getSpaceInfoAll)
GET  /api/confluence-mcp/spaces/{id}/pages # 하위 페이지 (getChild)
POST /api/confluence-mcp/search           # 콘텐츠 검색 (searchContent)
POST /api/confluence-mcp/import           # 선택한 페이지 임포트 (getPage × N)
GET  /api/confluence-mcp/user-pages       # 내가 작성한 페이지 (getUserContributedPages)
```

### 기존 코드 통합

| 기존 컴포넌트 | 통합 방법 |
|--------------|----------|
| `MetadataStore` | `source_type="confluence_mcp"`로 저장, 기존 스키마 그대로 활용 |
| `SyncEngine` | MCP 클라이언트용 sync 로직 추가 (주기적 동기화) |
| `ProcessingPipeline` | 변경 없음 — `pending` 상태로 저장되면 자동 처리 |
| `content_hash` | 동일한 SHA-256 해시로 변경 감지 |
| Web Dashboard | `/confluence-mcp` 페이지 추가 (탭 기반 UI) |

### REST API 방식 vs MCP 방식 비교

| 항목 | REST API (기존) | MCP Server (신규) |
|------|----------------|-------------------|
| 인증 | API 토큰/이메일 직접 관리 | MCP 서버가 인증 처리 |
| API 접근 | 직접 접근 필요 (차단됨) | MCP 서버가 중계 |
| HTML→MD 변환 | 자체 regex 변환 | MCP 서버가 변환해서 제공 가능 |
| 페이지네이션 | 직접 cursor 관리 | MCP 도구에 위임 |
| 추가 의존성 | `httpx` (이미 있음) | `mcp` (이미 있음) |

## 다음 TODO

- [ ] 사내 MCP 서버 전송 방식(SSE/stdio) 확인
- [ ] 각 도구의 입력 파라미터 및 반환 형식 확인
- [ ] 1순위(검색 기반 임포트) 구현
- [ ] 2순위(트리 탐색 임포트) 구현
- [ ] 3순위(내 문서 임포트) 구현
- [ ] SyncEngine 연동 (주기적 동기화)
