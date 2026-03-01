# Project Context Loop System

## 프로젝트 개요

사내 업무 소스(Confluence 등)를 개인 로컬 PC에 LLM context로 변환·저장하고, 변경사항을 자동 동기화하는 시스템이다.
CLI + 웹 UI 형태로 제공하며, `pip install`로 배포하고 사용자는 3개 명령어(`init`, `start`, `ask`)만으로 사용한다.

## 시스템 아키텍처

```
[Confluence / Jira / Slack / ...]
        │ (REST API + API Token)
        ▼
┌─────────────────┐
│  Connector Layer │  ← 소스별 어댑터 (플러그인 구조)
└────────┬────────┘
         ▼
┌─────────────────┐
│  Processor Layer │  ← 청킹, 임베딩, 요약
└────────┬────────┘
         ▼
┌─────────────────┐
│  Storage Layer   │  ← ChromaDB (벡터) + SQLite (메타데이터)
└────────┬────────┘
         ▼
┌─────────────────┐
│  Interface Layer │  ← CLI (click) + Web UI (Streamlit/Gradio)
└─────────────────┘
```

## 핵심 설계 원칙

1. **로컬 퍼스트**: 모든 데이터는 사용자 PC에 저장. 외부 서버 의존 최소화.
2. **증분 동기화**: 초기 full sync 이후에는 변경분(incremental)만 처리.
3. **플러그인 구조**: 소스 커넥터는 공통 인터페이스를 구현하는 플러그인으로 확장 가능.
4. **3단계 온보딩**: init → 소스 연결 → 초기 동기화. 사용자 설정은 3단계 이내로 완료.
5. **보안 우선**: 인증 토큰은 OS 키체인(keyring)에 저장. 설정 파일에 시크릿 노출 금지.

## 프로젝트 구조

```
context-sync/
├── pyproject.toml
├── README.md
├── src/
│   └── context_sync/
│       ├── __init__.py
│       ├── cli.py                  # CLI 진입점 (click 기반)
│       ├── config.py               # 설정 로드/저장
│       ├── auth.py                 # 토큰 관리 (keyring 연동)
│       ├── connectors/             # 소스 커넥터
│       │   ├── __init__.py
│       │   ├── base.py             # BaseConnector 추상 클래스
│       │   └── confluence.py       # Confluence 커넥터
│       ├── processor/              # 문서 처리
│       │   ├── __init__.py
│       │   ├── chunker.py          # 텍스트 청킹
│       │   ├── embedder.py         # 임베딩 생성
│       │   └── summarizer.py       # LLM 요약
│       ├── storage/                # 저장소
│       │   ├── __init__.py
│       │   ├── vector_store.py     # ChromaDB 래퍼
│       │   └── metadata_store.py   # SQLite 메타데이터
│       ├── sync/                   # 동기화 엔진
│       │   ├── __init__.py
│       │   ├── scheduler.py        # 주기적 동기화 스케줄러
│       │   └── engine.py           # 동기화 로직 (변경 감지, 업데이트, 삭제)
│       ├── query/                  # 질의 처리
│       │   ├── __init__.py
│       │   └── rag.py              # RAG 파이프라인 (검색 → 컨텍스트 조립 → LLM 호출)
│       └── web/                    # 웹 UI
│           ├── __init__.py
│           └── app.py              # Streamlit/Gradio 앱
├── tests/
│   ├── test_connectors/
│   ├── test_processor/
│   ├── test_storage/
│   └── test_sync/
└── config/
    └── default.yaml                # 기본 설정 템플릿
```

## 기술 스택

| 영역 | 기술 | 선택 이유 |
|------|------|-----------|
| CLI | click | 서브커맨드 구조, 풍부한 옵션 처리 |
| Web UI | Streamlit 또는 Gradio | Python만으로 UI 구현, 빠른 프로토타이핑 |
| 벡터 DB | ChromaDB | 로컬 임베디드 모드, pip install만으로 사용 |
| 메타데이터 DB | SQLite | 파일 기반, 별도 서버 불필요 |
| 임베딩 | OpenAI text-embedding-3-small 또는 로컬 모델 | 비용/성능 균형 |
| LLM | OpenAI GPT-4o 또는 Claude | RAG 응답 생성용 |
| 인증 저장 | keyring | OS 네이티브 키체인 연동 (Windows/macOS/Linux) |
| HTTP 클라이언트 | httpx | async 지원, 타임아웃 제어 용이 |
| 스케줄링 | APScheduler | 백그라운드 주기 실행 |
| 패키징 | pyproject.toml + hatchling | 모던 Python 패키징 표준 |

## 설정 파일 구조

```yaml
# ~/.context-sync/config.yaml

app:
  data_dir: "~/.context-sync/data"       # 로컬 DB 저장 경로
  log_level: "INFO"

sync:
  interval_minutes: 30                    # 동기화 주기
  max_concurrent: 3                       # 동시 처리 커넥터 수

sources:
  confluence:
    enabled: true
    base_url: "https://yourcompany.atlassian.net"
    email: "user@company.com"
    token_storage: "keyring"              # 토큰은 OS 키체인에 저장
    spaces:                               # 구독할 스페이스 목록
      - "DEV"
      - "PROJECT-A"
    exclude_labels:                       # 제외할 라벨
      - "draft"
      - "archived"

processor:
  chunk_size: 512                         # 청크 토큰 수
  chunk_overlap: 50                       # 청크 간 겹침
  embedding_model: "text-embedding-3-small"
  embedding_provider: "openai"            # "openai" | "local"

llm:
  provider: "openai"                      # "openai" | "anthropic"
  model: "gpt-4o"
  api_key_storage: "keyring"              # API 키도 키체인에 저장
  max_context_chunks: 10                  # RAG 시 최대 참조 청크 수

web:
  host: "127.0.0.1"
  port: 8501
```

## 커넥터 인터페이스

모든 소스 커넥터는 `BaseConnector`를 상속하여 구현한다.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator


@dataclass
class Document:
    """커넥터가 반환하는 문서 단위"""
    source_id: str              # 소스 시스템 내 고유 ID
    source_type: str            # "confluence", "jira", "slack" 등
    title: str
    content: str                # 본문 텍스트 (마크다운 또는 플레인텍스트)
    url: str                    # 원본 링크
    author: str
    updated_at: datetime
    metadata: dict              # 소스별 추가 정보 (스페이스, 라벨 등)


class BaseConnector(ABC):
    """소스 커넥터 공통 인터페이스"""

    @abstractmethod
    async def authenticate(self) -> bool:
        """인증 확인. 토큰 유효성 검증."""
        ...

    @abstractmethod
    async def fetch_all(self) -> AsyncIterator[Document]:
        """전체 문서 가져오기 (초기 동기화용)."""
        ...

    @abstractmethod
    async def fetch_updated(self, since: datetime) -> AsyncIterator[Document]:
        """특정 시점 이후 변경된 문서만 가져오기 (증분 동기화용)."""
        ...

    @abstractmethod
    async def fetch_deleted(self, since: datetime) -> list[str]:
        """특정 시점 이후 삭제된 문서 ID 목록."""
        ...
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
| 스페이스 목록 | `GET /wiki/api/v2/spaces` | 구독 대상 선택용 |
| 페이지 목록 | `GET /wiki/api/v2/spaces/{id}/pages` | 페이지네이션 필수 |
| 페이지 상세 | `GET /wiki/api/v2/pages/{id}?body-format=storage` | 본문 포함 |
| 변경 감지 | `GET /wiki/rest/api/content?expand=version&orderby=lastmodified` | `lastModifiedDate` 기준 |

### 변경 감지 전략

1. 메타데이터 DB에 각 문서의 `(source_id, version_number, last_modified)` 저장
2. 동기화 시 `orderby=lastmodified` + `modifiedDate >= last_sync_time`으로 변경분 조회
3. version_number 비교로 실제 변경 여부 확인
4. 로컬에 있지만 소스에서 삭제된 문서는 orphan cleanup 처리

## 동기화 엔진 로직

```
┌─────────────────────────────────────────────┐
│              Sync Cycle                      │
│                                             │
│  1. 마지막 동기화 시각 조회 (metadata DB)      │
│            │                                │
│  2. fetch_updated(since) 호출               │
│            │                                │
│  3. 변경 문서별:                              │
│     ├─ 신규 → 청킹 → 임베딩 → 벡터DB 저장    │
│     ├─ 수정 → 기존 청크 삭제 → 재생성         │
│     └─ 삭제 → 벡터DB + 메타DB에서 제거        │
│            │                                │
│  4. 동기화 시각 갱신                          │
│            │                                │
│  5. 로그 기록                                │
└─────────────────────────────────────────────┘
```

## CLI 명령어 체계

```bash
context-sync init             # 초기 설정 마법사 (웹 UI 실행)
context-sync start            # 백그라운드 동기화 + 웹 UI 시작
context-sync stop             # 백그라운드 동기화 중지
context-sync status           # 동기화 상태 확인
context-sync ask "<질문>"     # CLI에서 직접 질문
context-sync sync             # 수동 즉시 동기화
context-sync config           # 설정 웹 UI 열기
context-sync autostart        # 시스템 시작 시 자동 실행 등록/해제
```

## 메타데이터 DB 스키마 (SQLite)

```sql
-- 동기화된 문서 추적
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,          -- 소스 시스템 내 고유 ID
    source_type TEXT NOT NULL,        -- "confluence", "jira" 등
    title TEXT,
    url TEXT,
    author TEXT,
    version INTEGER,                  -- 소스 시스템의 버전 번호
    content_hash TEXT,                -- 본문 해시 (변경 감지용)
    last_synced_at TIMESTAMP,
    source_updated_at TIMESTAMP,
    UNIQUE(source_id, source_type)
);

-- 청크-문서 매핑
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,              -- 벡터DB의 chunk ID와 동일
    document_id INTEGER REFERENCES documents(id),
    chunk_index INTEGER,              -- 문서 내 청크 순서
    token_count INTEGER
);

-- 동기화 이력
CREATE TABLE sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,                       -- "success", "partial", "failed"
    docs_added INTEGER DEFAULT 0,
    docs_updated INTEGER DEFAULT 0,
    docs_deleted INTEGER DEFAULT 0,
    error_message TEXT
);
```

## 구현 순서

아래 순서로 점진적으로 구현한다. 각 단계가 독립적으로 동작 가능해야 한다.

### Phase 1: 기반 구조
1. 프로젝트 스캐폴딩 (pyproject.toml, 디렉토리 구조)
2. 설정 관리 (config.yaml 로드/저장, 기본값)
3. 인증 모듈 (keyring 연동, 토큰 저장/조회)

### Phase 2: Confluence 커넥터
4. BaseConnector 추상 클래스 구현
5. Confluence 커넥터 구현 (Cloud 우선)
6. 페이지네이션 처리, HTML → 마크다운 변환

### Phase 3: 문서 처리 파이프라인
7. 청킹 모듈 (토큰 기반 분할)
8. 임베딩 모듈 (OpenAI API 연동)
9. ChromaDB 벡터 저장소 래퍼
10. SQLite 메타데이터 저장소

### Phase 4: 동기화 엔진
11. 초기 전체 동기화 (full sync)
12. 증분 동기화 (변경 감지 + 반영)
13. 삭제 문서 정리 (orphan cleanup)
14. APScheduler 기반 주기 실행

### Phase 5: 질의 인터페이스
15. RAG 파이프라인 (검색 → 컨텍스트 조립 → LLM 호출)
16. CLI `ask` 명령어
17. 출처 표시 (소스 URL 링크)

### Phase 6: UI 및 배포
18. 웹 UI (대시보드, 채팅, 설정)
19. 초기 설정 마법사 (init 플로우)
20. 패키징 및 사내 배포

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
