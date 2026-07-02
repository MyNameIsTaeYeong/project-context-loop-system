# LLM Wiki 레이어 설계 — Context Loop System 적용안

> 상태: 설계 제안 (구현 전)
> 배경: Karpathy의 LLM Wiki 패턴(2026-04)을 본 시스템에 도입하기 위한 구체 설계.
> 관련 조사: MCP는 사장 기술이 아니며(2026-07-28 스펙 대개정 진행), LLM Wiki는
> MCP의 대체가 아니라 **RAG 지식 표현 층의 보완 패턴**이다. 위키 역시 기존 MCP 서버로 서빙한다.

## 1. 목표와 비목표

### 목표

1. 원본 문서(Confluence·git 코드)를 그때그때 검색하는 현재 RAG 위에, **LLM이 작성·유지하는 정제된 위키 레이어**를 추가한다.
2. 위키 페이지는 세션·동기화 주기를 넘어 **지식이 누적(compound)** 되도록 증분 갱신한다.
3. 기존 파이프라인(청킹→임베딩→그래프→저장)과 MCP 서빙을 **최대한 재사용**한다 — 위키는 새 `source_type`일 뿐이다.
4. 모든 위키 서술은 원본 문서 인용으로 **추적 가능**해야 한다 (환각 차단).

### 비목표

- 원본 RAG 검색의 대체 (위키가 못 다루는 롱테일 질의는 원본 청크가 계속 담당)
- MCP 프로토콜 교체 (위키도 기존 `search_context`/신규 도구로 서빙)
- 사람이 직접 편집하는 위키 (편집 주체는 LLM, 사람은 대시보드에서 승인/반려)

## 2. 패턴 ↔ 시스템 매핑

Karpathy 패턴의 3층 구조와 3연산을 본 시스템에 다음과 같이 대응시킨다.

| LLM Wiki 패턴 | 본 시스템 대응 | 비고 |
|---|---|---|
| `raw/` (불변 원본) | `documents` 테이블 (`original_content`/`raw_content`) | 이미 존재. content_hash로 버전 추적 중 |
| `wiki/` (LLM 생성 페이지) | **신규**: `data/wiki/*.md` + `documents(source_type="wiki")` 이중 저장 | 파일 = 진실 원본(git 버전 관리), DB 등록 = 검색 인덱싱용 |
| 스키마 문서 (CLAUDE.md) | **신규**: `data/wiki/_schema.md` + `wiki/schema.py` | 페이지 타입·frontmatter·링크 규약 정의 |
| `ingest` 연산 | **신규**: sync 완료 후 위키 갱신 배치 | `sync/engine.py` 후처리 훅 |
| `query` 연산 | 기존 `search_context` + 신규 `get_wiki_page` MCP 도구 | 위키 청크가 벡터/그래프 검색에 자연 편입 |
| `lint` 연산 | **신규**: `wiki/linter.py` + 대시보드 위키 탭 | stale/고아/무인용 검사 |

## 3. 아키텍처

```
                    ┌────────────────────────────────────────────┐
                    │            기존 수집·처리 (변경 없음)          │
  Confluence/Git ──▶│ sync engine → process_document → 청크/그래프 │
                    └──────────────────┬─────────────────────────┘
                                       │ ① 처리 완료 이벤트 (doc_id, content_hash 변경분)
                                       ▼
                    ┌────────────────────────────────────────────┐
                    │        신규: src/context_loop/wiki/          │
                    │                                            │
                    │  router.py    ② 변경 문서 → 영향 페이지 결정   │
                    │  synthesizer.py ③ LLM read-modify-write     │
                    │  linter.py    ④ 건강 검사 (stale/링크/인용)   │
                    │  store.py     ⑤ data/wiki/*.md 저장 +        │
                    │               documents(source_type="wiki") │
                    └──────────────────┬─────────────────────────┘
                                       │ ⑥ 승인 시 process_document("wiki")
                                       ▼
                    ┌────────────────────────────────────────────┐
                    │   기존 저장소: ChromaDB + SQLite + GraphStore │
                    └──────────────────┬─────────────────────────┘
                                       ▼
                    기존 MCP 서버 search_context / get_wiki_page(신규)
```

새 모듈 구성:

```
src/context_loop/wiki/
├── __init__.py
├── schema.py        # WikiPage 모델, frontmatter 파싱/검증, 페이지 타입 정의
├── store.py         # 파일 저장(data/wiki/) + documents 테이블 등록/갱신
├── router.py        # 변경 doc → 영향받는 위키 페이지 결정 (역인덱스 + 그래프 + 벡터)
├── synthesizer.py   # LLM 페이지 생성/증분 갱신 (인용 강제)
├── linter.py        # lint 규칙 실행, 결과 리포트
└── coordinator.py   # ingest 배치 오케스트레이션 (훅 진입점)
```

## 4. 데이터 모델

### 4.1 페이지 파일 형식

`data/wiki/<slug>.md` — YAML frontmatter + 마크다운 본문. 파일 디렉터리는 git으로 버전 관리하여 페이지 변경 이력을 무료로 확보한다.

```markdown
---
slug: payment-service
title: 결제 서비스
page_type: system          # concept | system | decision | guide | hub
entities:                  # graph_store 정규화 엔티티 (LOWER(name), type)
  - name: payment-service
    type: service
sources:                   # 이 페이지가 근거로 삼은 원본 문서
  - doc_id: 123
    content_hash: "a1b2..."   # 갱신 당시 해시 — stale 판정 기준
  - doc_id: 456
    content_hash: "c3d4..."
status: approved           # draft | approved | stale | archived
updated_at: 2026-07-02T09:00:00
wiki_version: 7            # synthesizer 갱신 횟수
---

## 개요

결제 서비스는 주문 완료 이벤트를 구독하여 PG사 결제를 수행한다 [doc:123].

## 아키텍처

`PaymentGateway` 클래스가 외부 PG API를 래핑하며 [doc:456], ...
```

규약:
- 모든 서술 문장은 `[doc:<id>]` 인용을 갖는다. 인용 없는 단락은 lint 에러.
- 페이지 간 상호 링크는 `[[slug]]` 표기 — 인덱싱 시 그래프 엣지(`wiki_links`)로 변환.
- 본문 최대 토큰: 6,000 (기존 doclevel 청킹 max 8,000 이내에서 1~2청크로 수렴하도록).

### 4.2 DB 등록

위키 페이지는 `store.py`가 `documents` 테이블에 등록한다:

| 컬럼 | 값 |
|---|---|
| `source_type` | `"wiki"` |
| `source_id` | slug |
| `original_content` | frontmatter 제외 본문 MD |
| `raw_content` | frontmatter 포함 전문 (원형 보존) |
| `content_hash` | 본문 해시 |
| `url` | `wiki://<slug>` (대시보드 라우팅용) |

기존 `UNIQUE(source_type, source_id)` 제약이 slug 유일성을 보장한다. **스키마 변경 불필요.**

frontmatter의 `sources` 역인덱스는 신규 테이블 하나만 추가한다:

```sql
CREATE TABLE IF NOT EXISTS wiki_page_sources (
    wiki_doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    source_doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    source_content_hash TEXT,          -- 갱신 당시 원본 해시
    PRIMARY KEY (wiki_doc_id, source_doc_id)
);
```

router의 "이 문서가 바뀌면 어느 페이지를 갱신하나"와 linter의 stale 판정이 이 테이블 조인 한 번으로 끝난다.

### 4.3 페이지 타입 (초기 5종)

| page_type | 시드 | 예시 |
|---|---|---|
| `concept` | 그래프 엔티티 (개념/용어) | "멀티뷰 임베딩", "정산 배치" |
| `system` | 그래프 엔티티 (service/module) + git_code FQN 클러스터 | "결제 서비스", "chunker 모듈" |
| `decision` | Confluence 결정 문서류 | "D-036 AST 정적 추출 채택" |
| `guide` | 서술형 가이드 문서 집약 | "신규 입사자 온보딩" |
| `hub` | 자동 생성 목차 (LLM 미사용) | 타입별/도메인별 인덱스 |

## 5. 세 연산 상세

### 5.1 ingest — 증분 갱신 파이프라인

**트리거**: `sync/engine.py`의 동기화 배치 완료 시점에 후처리로 1회 실행 (문서 1건마다 실행하지 않는다 — LLM 비용 제어). 수동 트리거는 대시보드 버튼 + CLI(`scripts/wiki_ingest.py`).

**흐름** (`coordinator.py`):

1. **변경 수집**: 직전 ingest 이후 `content_hash`가 바뀐 `documents` 목록 조회 (wiki 자신 제외).
2. **라우팅** (`router.py`) — 변경 문서 1건당 영향 페이지 결정, 3단 매칭:
   - a. `wiki_page_sources` 역인덱스 히트 (이미 인용 중인 페이지) — 확정 갱신 대상
   - b. `graph_node_documents` 조인: 변경 문서의 엔티티가 frontmatter `entities`와 정규화 일치하는 페이지
   - c. 벡터 유사도: 변경 문서 대표 청크 ↔ 위키 청크 top-3, 임계값 이상
   - a∪b∪c 공집합이면 **신규 페이지 후보 큐**에 적재 (엔티티 연결도가 설정 임계값 이상일 때만 생성).
3. **합성** (`synthesizer.py`) — 페이지 1건당 LLM 1회 호출 (read-modify-write):
   - 입력: 현재 페이지 전문 + 변경 문서 발췌(관련 청크만, 최대 N토큰) + 스키마 규약
   - 출력: 갱신된 페이지 전문. 프롬프트 제약: "원본에 없는 사실 추가 금지, 모든 신규 문장에 [doc:id] 인용, 기존 내용과 모순 발견 시 본문에 `> ⚠️ CONFLICT` 블록으로 표기"
   - LLM 클라이언트는 기존 `processor/llm_client.py` 재사용. 실패 시 기존 `llm_degraded` 패턴대로 페이지는 이전 버전 유지 + 결손 플래그.
4. **lint** (5.3) 통과 실패 시 저장 거부, 성공 시 `status: draft`로 저장.
5. **승인**: 대시보드 위키 탭에서 diff 확인 후 승인(→`approved`) 또는 반려. 설정 `wiki.auto_approve: true`이면 Judge 게이트(기존 `eval/llm.py`의 Generator/Judge 분리 인프라 재사용 — 합성 모델과 다른 모델로 채점) 통과 시 자동 승인.
6. **재인덱싱**: 승인 시점에 `process_document(doc_id)` 호출 → 위키 청크/임베딩/그래프가 기존 파이프라인으로 갱신된다.

**pipeline.py 분기**: `source_type == "wiki"`는 마크다운이고 `raw_content`가 HTML이 아니므로, 기존 else 분기에서 confluence 구조화 추출 조건(`source_type in ("confluence","confluence_mcp") and raw_html`)에 걸리지 않아 **upload와 동일한 doclevel 청킹 경로를 그대로 탄다.** 필요한 추가 처리는 두 가지뿐:
- `[[slug]]` 링크 → `wiki_links` 그래프 엣지 변환 (link_graph_builder에 위키 전용 소형 빌더 추가)
- frontmatter `entities` → 그래프 노드 연결 (LLM 호출 없이 결정론적)

### 5.2 query — 서빙 통합

**자연 편입**: 위키 청크가 ChromaDB에 들어가므로 기존 `search_context`가 추가 작업 없이 위키를 검색한다.

**랭킹 정책** (`context_assembler.py` 수정):
- 위키 청크와 그 위키가 인용한 원본 청크가 동시에 검색되면 **위키를 앞세우고 원본은 "근거" 섹션으로 강등** — 컨텍스트 중복 토큰 절약. 판정은 `wiki_page_sources` 조인.
- 위키 청크가 히트하면 해당 **페이지 전문을 첨부** — 기존 parent_document 로직(`mcp.parent_document_enabled`)이 그대로 적용된다 (위키 페이지는 6,000토큰 이하로 설계했으므로 전문 첨부 부담이 작다).
- 신선도 가드: `status: stale` 페이지는 컨텍스트에 "⚠️ 원본이 갱신되어 이 요약은 오래되었을 수 있음" 배너를 붙여 반환.

**신규 MCP 도구** (`mcp/tools.py`):

```
get_wiki_page(slug: str) -> str        # 페이지 전문 + 인용 원본 목록
list_wiki_pages(page_type: str|None)   # 목차 (hub 대체 API)
```

`search_context`에 `include_wiki: bool = True` 파라미터를 추가해 A/B 평가와 사용자 opt-out을 지원한다.

### 5.3 lint — 건강 검사

`linter.py` 규칙 (ingest 마지막 단계 + 야간 전수 배치):

| 규칙 | 판정 | 조치 |
|---|---|---|
| **stale** | `wiki_page_sources.source_content_hash` ≠ 현재 원본 hash | `status: stale` 전환, 다음 ingest 갱신 큐 |
| **broken-link** | `[[slug]]` 대상 페이지 부재 | 저장 거부 (ingest 시) / 리포트 (배치 시) |
| **no-citation** | 인용 없는 서술 단락 | 저장 거부 |
| **dead-citation** | `[doc:id]` 대상 문서 삭제됨 | stale 전환 + 해당 단락 표기 |
| **orphan** | 어떤 페이지·hub에서도 링크되지 않음 | 리포트 (hub 재생성 트리거) |
| **duplicate** | 페이지 간 제목/임베딩 유사도 임계 초과 | 리포트 (병합 후보) |
| **oversize** | 본문 > 6,000토큰 | 저장 거부 (분할 유도) |

lint 결과는 대시보드 위키 탭 배지 + `list_wiki_pages` 응답에 노출.

## 6. 설정 (config/default.yaml 추가분)

```yaml
wiki:
  enabled: false                  # 기본 off — Phase W1 완료 후 opt-in
  dir: "data/wiki"
  auto_approve: false             # true면 Judge 게이트 통과 시 자동 승인
  synthesis_model: null           # null이면 llm.model 상속
  judge_model: null               # auto_approve용 — synthesis와 달라야 함 (self-eval 편향 차단)
  max_page_tokens: 6000
  max_source_excerpt_tokens: 12000
  new_page_min_entity_degree: 5   # 신규 페이지 자동 생성 임계 (엔티티 연결도)
  ingest_batch_llm_limit: 50      # ingest 1회당 최대 합성 호출 수 (비용 상한)
  router_similarity_threshold: 0.45
```

## 7. 단계별 구현 계획

| 단계 | 범위 | 산출물 | 완료 판정 |
|---|---|---|---|
| **W1 (MVP)** | schema/store/synthesizer + **배치 초기 생성**: 그래프 연결도 상위 30개 엔티티 → concept/system 페이지. `source_type="wiki"` 인덱싱. `get_wiki_page` 도구 | `wiki/` 모듈, `scripts/wiki_bootstrap.py` | 위키 청크가 `search_context` 결과에 등장, 인용 lint 통과율 100% |
| **W2** | 증분 ingest (sync 훅 + router) + linter 전체 규칙 + 대시보드 위키 탭(초안 diff 승인 UI) | `coordinator.py`, `linter.py`, web/api 위키 라우트 | 원본 변경 → 다음 sync에서 해당 페이지 stale→갱신 확인 |
| **W3** | 랭킹 정책(위키 우선/원본 강등) + Judge 자동 승인 + **평가 확장**: 기존 골드셋으로 `include_wiki` on/off A/B (eval_search 재사용), 위키 커버리지·신선도 메트릭 | context_assembler 수정, eval 확장 | A/B에서 위키 on이 answer 품질 저하 없이 컨텍스트 토큰 절감 확인 |
| **W4 (선택)** | 모순 감지 고도화(CONFLICT 블록 → 대시보드 이슈화), decision/guide 타입, hub 자동 생성 | — | — |

## 8. 리스크와 대응

| 리스크 | 대응 |
|---|---|
| 합성 환각 (원본에 없는 서술) | 인용 강제 lint + Judge 게이트 + 기본 수동 승인. 인용은 원본 청크와 대조 가능 |
| LLM 비용 증가 | ingest는 sync당 1배치, `ingest_batch_llm_limit` 상한, 페이지당 1호출 설계 |
| 위키·원본 중복으로 컨텍스트 오염 | 랭킹 강등 정책(§5.2) + A/B 평가(W3)로 정량 검증 후 확대 |
| 페이지 폭증 (엔티티마다 생성) | `new_page_min_entity_degree` 임계 + 신규 생성은 후보 큐 → 사람 승인 |
| stale 위키가 낡은 답 유도 | content_hash 기반 stale 전환 + 컨텍스트 배너 + 다음 sync 자동 갱신 |
| Judge 자동 승인의 self-eval 편향 | `judge_model ≠ synthesis_model` 강제 (기존 rag-eval-audit 교훈 재적용) |

## 9. 기존 코드 접점 요약 (수정 파일 목록)

| 파일 | 변경 | 단계 |
|---|---|---|
| `src/context_loop/wiki/*` | 신규 모듈 6파일 | W1–W2 |
| `storage/metadata_store.py` | `wiki_page_sources` 테이블 추가 | W1 |
| `processor/pipeline.py` | `source_type=="wiki"` 후처리(링크→엣지, entities→노드) | W1 |
| `processor/link_graph_builder.py` | `[[slug]]` 위키 링크 빌더 | W1 |
| `mcp/tools.py` | `get_wiki_page`, `list_wiki_pages`, `search_context(include_wiki=)` | W1 |
| `mcp/context_assembler.py` | 위키 우선/원본 강등 랭킹, stale 배너 | W3 |
| `sync/engine.py` | 배치 완료 훅 → `wiki.coordinator.run_ingest()` | W2 |
| `web/api/*`, `templates/*` | 위키 탭 (목록/diff/승인) | W2 |
| `config/default.yaml` | `wiki.*` 블록 | W1 |
| `scripts/wiki_bootstrap.py` | 초기 페이지 배치 생성 CLI | W1 |
| `scripts/eval_search.py` | `--include-wiki` A/B 플래그 | W3 |
