# 로컬 환경 설정 및 실행 가이드

## 사전 요구사항

- Python 3.11 이상
- Git

```bash
python3 --version  # 3.11+ 확인
git --version
```

## 1. 저장소 클론

```bash
git clone <저장소_URL>
cd project-context-loop-system
```

## 2. Python 가상환경 생성 및 패키지 설치

```bash
# 가상환경 생성
python3 -m venv .venv

# 활성화 (macOS/Linux)
source .venv/bin/activate

# 활성화 (Windows)
.venv\Scripts\activate

# 패키지 설치
pip install -e .

# 개발 도구 포함 설치 (선택)
pip install -e ".[dev]"
```

## 3. 설정 파일 초기화

```bash
mkdir -p ~/.context-loop
cp config/default.yaml ~/.context-loop/config.yaml
```

`~/.context-loop/config.yaml`을 열어 Confluence 연동 시 아래 항목 수정:

```yaml
sources:
  confluence:
    base_url: "https://yourcompany.atlassian.net"
    email: "user@company.com"
```

## 4. API 키 등록 (OS 키체인)

API 키는 설정 파일이 아닌 **OS 키체인(keyring)**에 저장한다.

```bash
# OpenAI API 키
python3 -c "import keyring; keyring.set_password('context-loop', 'openai_api_key', 'sk-...')"

# Anthropic API 키 (anthropic provider 사용 시)
python3 -c "import keyring; keyring.set_password('context-loop', 'anthropic_api_key', 'sk-ant-...')"

# Confluence 토큰 (Confluence 연동 시)
python3 -c "import keyring; keyring.set_password('context-loop', 'confluence_token', '여기에_토큰')"
```

## 5. 웹 대시보드 실행

`web/app.py`는 `create_app()` 팩토리 함수 방식으로 구현되어 있으므로, `--factory` 플래그를 반드시 사용한다.

```bash
python3 -m uvicorn "context_loop.web.app:create_app" --factory --host 127.0.0.1 --port 8000 --reload
```

> **주의**: `context_loop.web.app:app` 형태로 실행하면 `Attribute "app" not found` 에러가 발생한다.
> 반드시 `create_app` 팩토리 함수와 `--factory` 플래그를 함께 사용할 것.

브라우저에서 `http://127.0.0.1:8000` 접속

## 6. MCP Server 실행 및 Claude Code 연동

> **선행 조건**: MCP 서버는 시작 시점에 `~/.context-loop/data`의 인덱스를
> 한 번 메모리로 로드한다. 따라서 **먼저 웹 대시보드나 `scripts/`로 문서/코드를
> 1건 이상 인덱싱**해 두어야 검색 결과가 나온다. 인덱싱을 추가한 뒤에는
> 서버를 **재시작**해야 반영된다.

### 6.1 서버 직접 실행 (동작 확인용)

```bash
# stdio 전송 (로컬 코딩 에이전트 연동의 기본)
context-loop mcp serve

# 또는 콘솔 스크립트 없이
python3 -m context_loop.mcp serve

# SSE 전송 (원격/팀 공유)
context-loop mcp serve --transport sse --port 3001
```

> stdio 모드에서는 stdout이 JSON-RPC 채널이므로 직접 띄워도 화면에 아무것도
> 출력되지 않는 것이 정상이다(로그는 stderr로 나간다). 종료는 `Ctrl+C`.

### 6.2 Claude Code에 등록

프로젝트 루트의 `.mcp.json.example`을 `.mcp.json`으로 복사해 사용한다
(`.mcp.json`은 git에 커밋하면 팀 전체가 공유, `--scope project`).

```bash
cp .mcp.json.example .mcp.json
# .venv 경로가 다르면 CONTEXT_LOOP_VENV 환경변수로 지정
export CONTEXT_LOOP_VENV=/절대경로/project-context-loop-system/.venv
```

`.mcp.json` 내용(절대경로로 직접 적어도 된다):

```json
{
  "mcpServers": {
    "context-loop": {
      "command": "/절대경로/.venv/bin/context-loop",
      "args": ["mcp", "serve"],
      "env": {}
    }
  }
}
```

또는 CLI로 등록:

```bash
claude mcp add --scope project context-loop -- /절대경로/.venv/bin/context-loop mcp serve
```

등록 후 Claude Code에서 `/mcp`로 `context-loop` 서버와 도구 4종
(`search_context`, `list_documents`, `get_document`, `get_graph_context`)이
보이면 연결 완료다.

## 현재 구현 상태

| Phase | 내용 | 상태 |
|-------|------|------|
| Phase 1 | 기반 구조 | 완료 |
| Phase 2 | 문서 입력 파이프라인 | 완료 |
| Phase 3 | LLM 처리 | 완료 |
| Phase 4 | 웹 대시보드 | 완료 |
| Phase 5 | MCP Server | 완료 (`context-loop mcp serve`) |
| Phase 6-7 | 질의 고도화 / 배포 | 미구현 |

현재 웹 대시보드(`http://127.0.0.1:8000`)를 통해 모든 기능을 사용할 수 있다.
