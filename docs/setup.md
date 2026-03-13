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

## 6. MCP Server 설정 (Claude Code 연동 시)

> Phase 5 구현 완료 후 사용 가능

Claude Code의 MCP 설정 파일에 추가:

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

## 현재 구현 상태

| Phase | 내용 | 상태 |
|-------|------|------|
| Phase 1 | 기반 구조 | 완료 |
| Phase 2 | 문서 입력 파이프라인 | 완료 |
| Phase 3 | LLM 처리 | 완료 |
| Phase 4 | 웹 대시보드 | 완료 |
| Phase 5 | MCP Server | 미구현 |
| Phase 6-7 | 질의 고도화 / 배포 | 미구현 |

현재 웹 대시보드(`http://127.0.0.1:8000`)를 통해 모든 기능을 사용할 수 있다.
