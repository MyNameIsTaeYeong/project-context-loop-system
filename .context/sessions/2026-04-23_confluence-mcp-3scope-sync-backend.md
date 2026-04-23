# Confluence MCP 3-scope 싱크 백엔드 구현

- 일시: 2026-04-23
- 브랜치: `claude/confluence-continuous-fetch-aOGCB`
- 관련 결정: D-043 (신규)
- 관련 이슈: I-010 (부분 진전), I-030 (신규 — UI 잔여)

## 배경 / 목표

사용자 요청: "특정 공간 일부 문서를 지속적으로 가져오기". 3가지 범위를 모두 커버:
1. 📄 **페이지 단건** — 특정 페이지 하나만
2. 🌿 **서브트리** — 특정 페이지부터 하위 전부
3. 🏢 **공간 전체** — 특정 공간의 모든 페이지

진입 UX는 **검색 기반**, 실행은 **버튼 트리거**. 사내 Confluence REST API 차단(I-011)으로 기존 `SyncEngine`(REST)을 확장할 수 없어 MCP 전용 경로를 처음부터 새로 구성.

## 설계 합의 (이번 세션 상반부)

사용자와 4개 결정을 확정:

1. `searchContent` envelope 에 `totalSize`/`size` 가 함께 온다 → 공간 전체 싱크 다이얼로그에 예상 페이지 수 표기 가능
2. `getPageById(expand=ancestors,space)` 로 ancestors 를 받아 breadcrumb 해석 가능
3. **해제 시 임포트된 문서도 삭제** (그대로 두기/stale 마크 대신)
4. **공간 전체 + 서브트리 동시 등록 허용** — membership 기반 참조 카운팅으로 공유 페이지 안전성 확보

## 구현된 9단계 (모두 커밋 + 테스트 동반)

### 1. `delete_document_cascade` facade (commit 2410a80)
`web/api/documents.py:309-311` 에 인라인되어 있던 "vector → graph → meta" 3단 삭제 순서를 `storage/cascade.py` 로 추출. 해제 시 고아 문서 삭제와 문서 관리 API가 같은 경로를 공유. 문서 부재 시 False 반환으로 호출측의 404 판정을 단순화.

### 2. `SearchEnvelope` + `search_content_envelope` (commit 3b908ac)
기존 `search_content` 는 `results` 만 반환해 envelope 메타가 유실. DTO + 별도 함수를 신설해 `totalSize`/`size`/`start`/`limit` 보존. `total`/`totalSize`/`_totalSize` 변종, envelope 없는 list-only 응답, 빈/텍스트 응답 3경로 모두 처리.

### 3. `get_page_with_ancestors` + `format_breadcrumb` (commit fc20735)
`expand=ancestors,space,version` 으로 본문 제외 경량 조회. `format_breadcrumb` 은 `space.name → ancestors[].title → page.title` 을 `" / "` 로 결합, space.name 없으면 key 폴백, 빈 항목 스킵, 모두 비면 id 폴백.

### 4. `walk_subtree` BFS walker (commit d564eec)
`getChild` 재귀 호출로 서브트리 전체를 `{id, parent_id, depth, title}` 로 평탄화. 안전장치: 방문 집합(사이클 차단), `type != "page"` 필터(blogpost/attachment 스킵), `max_depth=20`/`max_pages=5000` 상한, 가지별 `getChild` 실패 격리. 루트는 `parent_id=None, depth=0, title=""` 로 첫 항목.

### 5. `estimate_space_page_count` + `enumerate_space_pages` (commit 30e0e6a)
- 추정: CQL `space = "KEY" AND type = "page"` 를 `limit=1` 로 호출해 `totalSize` 만 추출
- 열거: 같은 CQL 로 페이지네이션 반복. 종료 조건 3중 — `total_size` 도달 / `size < page_size` (마지막 페이지) / 빈 응답. `max_pages=10000` 상한.
- async generator 로 yield — 수천 페이지 스페이스의 메모리 부담 완화
- `_space_cql` 헬퍼에서 `\` / `"` escape

### 6. 스키마: `confluence_sync_targets` + `confluence_sync_membership` (commit 899b5f6)
```sql
confluence_sync_targets(id, scope CHECK IN (page|subtree|space), space_key, page_id, name, created_at, last_sync_at, last_result_json)
UNIQUE INDEX (scope, space_key, COALESCE(page_id, ''))  -- NULL collapse

confluence_sync_membership(target_id FK CASCADE, page_id, space_key, parent_page_id, depth, last_seen_at)
PRIMARY KEY (target_id, page_id)
INDEX (page_id), INDEX (space_key)
```
`COALESCE` 표현식 인덱스를 쓰는 이유: SQLite 기본 UNIQUE 가 NULL 을 distinct 로 취급해 space scope 중복을 허용하기 때문. 같은 page 가 여러 target 에 속할 수 있도록 membership PK 는 `(target_id, page_id)` 복합.

### 7. CRUD 메서드 + orphan 감지 (commit 6d15192)
`MetadataStore` 에 8개 메서드:
- targets: `upsert_sync_target`, `get_sync_target`, `list_sync_targets` (`created_at DESC, id DESC` 타이브레이크), `update_sync_result`, `delete_sync_target`
- membership: `upsert_membership`, `upsert_membership_batch`, `list_membership_page_ids`, `remove_memberships`

핵심은 `_find_orphans_if_membership_dropped(target_id, page_ids)` — 가상 삭제 시 "다른 어떤 target 도 이 page 를 소유하지 않음" 을 쿼리로 판정해 cascade 삭제 대상 doc_id 만 반환. 실제 문서 cascade 는 호출측(`delete_document_cascade`) 이 담당 — 트랜잭션 안전.

`delete_sync_target` 은 `(deleted, orphan_doc_ids)` 튜플 반환 — FK CASCADE 로 membership 이 자동 사라지기 전에 orphan 후보를 계산해 반환.

### 8. `execute_sync_target` 디스패처 + 3-scope 실행 (commit a7d94cc)
`src/context_loop/sync/mcp_sync.py` 신규. scope 별 3개 핸들러:
- `_sync_page`: 단건 import + membership upsert
- `_sync_subtree`: walk → import loop → `_prune_stale_memberships`
- `_sync_space`: enumerate → import loop → `_prune_stale_memberships` (hierarchy 저장 안 함)

**두 개의 안전 속성 명시적 구현**:
1. 열거 실패(walker/enumerate 예외) 시 membership 을 수정하지 않고 반환 — 일시 장애가 cascade 삭제로 번지지 않도록.
2. 임포트 단계의 개별 실패도 stale 삭제를 유발하지 않음. `current_ids.add()` 를 import try 블록 **앞**에 두어, walker 가 확인한 페이지는 import 성공 여부와 무관하게 stale 판정에서 제외됨.

### 9. 웹 API 엔드포인트 7종 (commit 2b233b0)
| Method | Path | 역할 |
|---|---|---|
| GET | `/api/confluence-mcp/search?q=...` | spaces+pages 병합 응답. 빈 q 는 공간 전체 나열 |
| GET | `/api/confluence-mcp/spaces/{key}/estimate` | 확인 다이얼로그용 예상 페이지 수 |
| POST | `/api/confluence-mcp/sync-targets` | 등록 + 백그라운드 첫 싱크. name 자동 해석 (breadcrumb / space.name) |
| GET | `/api/confluence-mcp/sync-targets` | 목록 + 각 진행 상태 |
| GET | `/api/confluence-mcp/sync-targets/{id}` | 단건 폴링 |
| POST | `/api/confluence-mcp/sync-targets/{id}/sync` | 재싱크 (lock 충돌 시 409) |
| DELETE | `/api/confluence-mcp/sync-targets/{id}` | 해제 + orphan cascade 삭제 |

동시성: `target_id → asyncio.Lock` 모듈 레벨 dict. `_target_status` 에 running/completed/failed + elapsed 기록. `BackgroundTasks` 로 응답 후 실제 sync 실행.

보조로 `get_space_info(session, space_key)` MCP 래퍼도 추가 — 공간 등록 시 space.name 을 해석하기 위한 도구.

## 테스트

| 대상 | 신규 건수 |
|---|---|
| `test_storage/test_cascade.py` | 4 |
| `test_storage/test_metadata_confluence_sync.py` | 31 (스키마 13 + CRUD 18) |
| `test_ingestion/test_mcp_confluence.py` | +42 (envelope 7, ancestors/breadcrumb 12, walker 10, space enum 13, get_space_info 2) — 기존 41 → 83 |
| `test_sync/test_mcp_sync.py` | 14 |
| `test_web/test_confluence_mcp_sync_api.py` | 19 |
| **합계** | **110 건 신규, 모두 통과** |

핵심 안전 속성이 테스트로 고정됨:
- walker/enumerate 실패 시 membership 보존
- 개별 import 실패가 stale 삭제로 번지지 않음
- 공유 페이지: 한쪽 해제 → 유지, 양쪽 해제 → cascade 삭제

전체 스위트(기존 + 신규): 208건 pass. 기존 실패 14건은 사전 Jinja2 환경 문제(회귀 아님).

## 한계 / 다음 작업

- **UI 미구현** (10단계) — 검색 박스, 3버튼 카드, 확인 다이얼로그, 등록된 대상 카드 + 폴링 진행률. `web/templates/confluence_mcp.html` 확장 + vanilla JS 필요 (I-030 신규 등록).
- **자동 주기 실행** — 현재는 버튼 트리거만. `MCPSyncEngine` 으로 `auto_sync_enabled` 토글 기반 주기 실행은 후순위.
- **SSE 진행률** — 현재 폴링 기반. 수천 페이지 공간 싱크의 UX 개선에 유용할 수 있으나 필수는 아님.
- **I-010(3가지 임포트 시나리오: 검색/트리/내 문서)** — 검색 기반은 이번에 완성(백엔드 + 부분 UI), 트리 탐색은 walker 가 준비되어 있으나 탐색형 UI 미구현, 내 문서(`getUserContributedPages`)는 기존 수동 임포트 엔드포인트만 존재.

## 아키텍처 성질

**"커널" + "러너" 분리**: `execute_sync_target` 은 session/stores 만 받으면 돌아가는 순수 함수. 웹 API 의 BackgroundTasks 가 이를 호출하지만, 향후 `MCPSyncEngine` (주기 루프) / CLI 트리거 / 테스트 등 어떤 러너에서도 동일한 경로를 재사용할 수 있음.

**스토어 책임 분리**: MetadataStore 는 membership diff 만 계산하고 orphan doc_id 를 반환. 실제 vector/graph/meta cascade 삭제는 storage facade(`delete_document_cascade`) 가 담당. 두 계층이 분리되어 있어 트랜잭션 반쪽 실패 시 재시도·복구 로직을 나중에 끼워 넣기 쉬움.
