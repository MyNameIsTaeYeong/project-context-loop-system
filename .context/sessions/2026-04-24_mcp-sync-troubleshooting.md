# Confluence MCP 싱크 — UI 배포 후 트러블슈팅 체인

- 일시: 2026-04-24 (후반부)
- 브랜치: `claude/plan-document-sync-ui-aOBNT`
- 관련 결정: D-044 (신규 — subtree CQL 전환 + 2-phase 싱크)
- 관련 이슈: I-031 ~ I-035 (모두 해결)
- 커밋 체인: `a6a928c → de0d7fb → 9536c0c → 3bb7685 → d507b15 → 49add3c → 2178564 → c5c9ca6`

## 배경

전일 백엔드 완성 + 같은 날 오전 UI 배포(`a6a928c`, I-030 해결). UI 로 실제 "하위 포함" 싱크를 돌려본 직후부터 연쇄적으로 드러난 5개 이슈를 순차 수정했다. 각 이슈는 **사용자 피드백 → 가설 → 검증 → 수정 → 회귀 테스트** 사이클로 처리했고, 마지막 단계에서 실패 문서 자동 재시도와 Phase 2 동시성까지 확장.

## 이슈 1: MCP tool 필수 파라미터 검증 에러 (I-031)

**증상**: UI 에서 공간 검색 클릭 → `CallToolResult validation` 에러. 서버가 `getSpaceInfoAll` 에 `start`/`limit` 필수로 요구.

**근본 원인**: 사내 MCP 서버 스펙에 페이지네이션 파라미터가 필수로 선언됨. 기존 `get_all_spaces` 는 `{}` 만 전달.

**수정 (`de0d7fb`)**:
- `get_all_spaces(session, *, page_size=100, max_spaces=10000)` — `{"start": 0, "limit": 100}` 전달 + envelope 응답 페이지네이션
- envelope 없이 list 만 오는 서버 변종도 한 번에 처리 후 종료
- 테스트 +3 (envelope totalSize, size<page_size, empty)

**후속 발견**: 같은 날 사용자가 `getChild` 에도 동일 이슈 보고 (I-031 연장).

**수정 (`9536c0c`)**:
- `get_child_pages(session, page_id, *, page_size=100, max_children=5000, expand="")` — `pageId` 외에 `start`/`limit`/`expand` 함께 전달
- `walk_subtree` 테스트 fake 가 이미 `**args` 로 받아서 호환

## 이슈 2: 서브트리 `하위 포함` 클릭 시 루트만 임포트 (I-032)

**증상**: 하위 포함 버튼으로 페이지 등록 → 루트만 임포트되고 자식 전부 누락.

**가설 탐색**:
1. `getChild` 응답 형태가 다름? — `CallToolResult` 스키마 확인 필요
2. `expand=""` 거부? — 서버 스펙 불명확
3. 응답 키가 `results` 가 아닌 다른 이름?

**근본 원인 2 가지 동시에**:
1. **`structuredContent` 채널 누락**: MCP 신규 스펙은 JSON 을 `CallToolResult.structuredContent` 에 직접 담기도 하는데, `_parse_json_result` 는 `content[].text` 만 읽어서 전체가 소실됨. `python -c "from mcp.types import CallToolResult; print(CallToolResult.model_fields)"` 로 실제 필드 확인.
2. **envelope 키 변형**: 서버 구현체마다 `results` / `children` / `page` / `pages` / `items` 또는 `{page: {results: [...]}}` 중첩 형태로 다름.

**수정 (`3bb7685`)**:
- `_parse_json_result` 가 `structuredContent` 를 먼저 확인
- `_unwrap_envelope(parsed) -> (items, envelope)` 공통 헬퍼 신설 — 5가지 키 변종 + 1단계 중첩 모두 풀어냄
- `get_child_pages` / `get_all_spaces` 를 공통 헬퍼로 리팩터링
- `expand` 기본값 `""` → `"page"` (빈 문자열을 거부하는 서버 방어)
- 테스트 +4 (structuredContent 우선, children 키, 중첩 page.results, expand 기본값 non-empty)

## 이슈 3: 서브트리가 최하위 depth 까지 가져오지 않음 (I-033)

**증상**: BFS walker 로 수정된 뒤에도 일부 깊은 페이지가 누락.

**사용자 제안**: "searchContent 툴에서 `ancestor` + `type` 으로 `totalCount` 를 받을 수 있다" → CQL `ancestor = X AND type = "page"` 로 평탄 열거.

**검토 결과**: 제안이 맞음. BFS 의 누락 경로 5가지:
1. per-parent 페이지네이션 각각이 독립 — 한 곳 종료 오판 시 아래 가지 전부 손실
2. 중간 노드 `getChild` 예외 격리로 그 아래 서브트리 손실
3. `max_depth=20` 초과
4. `type` 필드가 `"page"` 아닌 값/누락으로 드롭
5. 권한 차이 per-node 호출

CQL `ancestor` 는 서버 측에서 **직·간접 모든 조상 관계** 로 평가되어 depth 무관 완전 열거.

**수정 (`d507b15`)**:
- `_subtree_cql(id)` — 이스케이프 포함
- `estimate_subtree_page_count(session, id)` — envelope.totalSize 반환
- `enumerate_subtree_pages(session, id)` — async generator, `enumerate_space_pages` 와 동일 패턴
- `_sync_subtree` 가 walker 대신 CQL descendants + 루트 수동 prepend (ancestor 결과엔 루트 자신 미포함)
- Trade-off: membership 의 `parent_page_id`/`depth` 컬럼이 NULL 로 저장됨 — 현재 코드베이스에서 해당 컬럼을 읽는 곳이 없어 실영향 없음
- `walk_subtree` 는 삭제하지 않고 유지 (다른 러너 호환)
- 테스트 +9 (_subtree_cql 이스케이프, estimate 2, enumerate 6), 기존 subtree sync 테스트 6건 재작성

## 이슈 4: totalCount 만큼 임포트되지 않음 — 페이지네이션 버그 (I-034)

**증상**: CQL 이 totalSize=356 을 돌려주는데 실제 임포트는 그보다 적음.

**근본 원인 2개 버그 협력**:

**Bug A**: `size < page_size` 에서 무조건 break
- 일부 서버가 요청 `limit` 을 무시하고 응답당 개수를 cap (예: `limit=100` 요청에 `size=25` 응답 + `totalSize=500`)
- 기존 로직은 첫 응답 직후 short-page 로 판단 → 475건 누락

**Bug B**: `start += page_size` (요청값 증분)
- 서버 cap 으로 25개만 왔는데 start 를 100 증가 → items 25–99 스킵

**수정 (`49add3c`)**:
- `_paginate_cql(session, cql, *, page_size, max_pages, label)` 공통 헬퍼 추출 (enumerate_space_pages / enumerate_subtree_pages 중복 제거 + 동시 수정)
- Bug A 수정: `total_size` 가 알려진 경우 short-page 휴리스틱 skip, `total_size` 없을 때만 적용
- Bug B 수정: `start += env.size if env.size > 0 else len(env.results)` — 실제 반환 개수만큼 전진
- 관측성 보강: `_sync_subtree` 가 `estimate_subtree_page_count` 로 totalSize 를 먼저 받아두고 실제 열거 수와 비교, 불일치 시 warning 로그
- 테스트 +2 (`_make_capping_search_session` fake 로 server_cap 시나리오 재현 — space/subtree 각 1건)

## 이슈 5: 싱크 후 수동 "Process" 클릭 필요 (I-035)

**증상**: 싱크 완료 후 문서는 meta 에 저장되지만 검색 불가 — 사용자가 각 문서의 "Process" 버튼을 일일이 클릭해야 벡터/그래프 인덱싱.

**해결 (`2178564`)**: Phase 1 + Phase 2 통합 (D-044 참조)
- `execute_sync_target` 에 선택적 `embedding_client`/`pipeline_config` 주입 → Phase 2(process_document) 자동 실행
- `SyncResult` 에 `processed`/`processing_errors` 버킷 추가
- UI `summaryFor()` 에 `◎N indexed · ⚠M indexing-failed` 두 조각
- 사용자 UX 는 동일 (한 버튼 = 한 작업), 내부만 2단계
- 테스트 +5

**부가 해결 (`c5c9ca6`)**: 동시성 + 실패 재시도
- `asyncio.Semaphore(5)` + `asyncio.gather` 로 Phase 2 병렬화 — 400 문서 기준 벽시계 ~5배 단축
- `config.processor.phase2_concurrency` 로 운영자 튜닝 가능. 0 이하는 1로 clamp
- 기존에는 Phase 2 에서 실패한 문서가 재싱크 시 `unchanged` 로 분류되어 자동 재처리되지 않던 문제 해결
- `MetadataStore.list_failed_member_doc_ids(target_id)` — 신규 JOIN 쿼리로 target 스코프 내 status='failed' 문서 식별
- Phase 2 큐 = `created + updated + failed-in-membership` (중복 제거)
- 테스트 +6 (failed_member JOIN 2건, 동시성 bound 검증 + 실제 겹침 관측, failed 재시도, concurrency=0 clamp)

## 학습 / 아키텍처 성질

**서버 스펙 불확실성에 대응하는 헬퍼 계층**

MCP 서버 응답 형태가 (content.text vs structuredContent) × (envelope 키 변종) × (server cap 여부) 조합으로 다수 존재. 각 call site 에서 분기하는 대신 한 곳(`_parse_json_result`, `_unwrap_envelope`, `_paginate_cql`)에서 흡수. 이후 새 MCP 도구 추가 시 이 헬퍼만 믿으면 됨.

**열거 전략은 비즈니스 로직이 아닌 성능·정확성의 선택**

walk_subtree(BFS) 와 enumerate_subtree_pages(CQL) 는 동일한 추상(= "루트 아래 모든 페이지") 의 두 구현. `_sync_subtree` 는 구현을 CQL 로 교체하되, walker 자체는 삭제하지 않고 유지 — 향후 hierarchy 가 필요한 러너가 나타나면 재활용 가능.

**2-phase 분리는 `SyncResult` 가 "결과 스키마" 역할을 함**

Phase 1 결과 버킷(created/updated/unchanged)이 Phase 2 큐의 입력 스펙이 됨. 외부에서 새 Phase 3(예: "인덱싱된 문서를 즉시 재랭크 학습") 을 끼워 넣어도 같은 계약을 따르게 된다.

**failed 재시도는 "정상 idempotent 재싱크" 의 일부로 편입**

별도 "재인덱스" 버튼이나 cron 없이, 사용자가 평소 하던 재싱크 행위에 자동으로 실패 복구가 녹아듦. membership 테이블이 이미 target 스코프의 문서를 권위 있게 가리키므로 JOIN 한 번으로 깔끔하게 식별.

## 테스트 통계

| 커밋 | 신규 테스트 | 누적 통과 |
|---|---|---|
| `de0d7fb` | +3 (get_all_spaces pagination) | 107 |
| `9536c0c` | +2 (get_child_pages pagination) | 109 |
| `3bb7685` | +4 (structuredContent, envelope variants, expand) | 113 |
| `d507b15` | +9 (_subtree_cql, estimate, enumerate, 6 sync) | 136 |
| `49add3c` | +2 (server cap) | 138 |
| `2178564` | +5 (Phase 2 기본, unchanged skip, 실패 격리, skip 조건, summary) | 142 |
| `c5c9ca6` | +6 (failed_member JOIN 2, 동시성 bound, failed 재시도, clamp) | 178 |

## 성능 특성 요약 (400 문서 기준)

- **첫 싱크**: Phase 1 ~13분(직렬 MCP) + Phase 2 ~4분(concurrency=5) ≈ 17분
- **재싱크 (변경 없음)**: Phase 1 ~13분 (본문 해시 비교용 재페치) + Phase 2 0 (unchanged skip) ≈ 13분
- **MCP 호출 수 (재싱크)**: 1(estimate) + 4(enumerate 100+100+100+56) + 356(getPageByID) = 361
- **임베딩 API 호출 수 (재싱크 변경없음)**: 0

## 한계 / 다음 작업

- **Phase 1 직렬** — MCP 호출도 Semaphore 로 병렬화하면 재싱크 시간 ~1/5. MCP 서버 rate limit 확인 필요.
- **버전 기반 early-skip** — CQL 열거 시 `version.number` 를 받아 저장된 버전과 같으면 `getPageByID` 생략. 재싱크 시간 13분 → ~1분으로 단축. 스키마 변경(`source_version` 컬럼) 필요.
- **배치 임베딩** — 현재는 문서 단위 `aembed_documents`. 여러 문서의 청크를 한 배치로 묶으면 HTTP 왕복 수 감소. pipeline.py 수정 필요.
- **싱크 중 프로세스 재시작 복구** — 현재 `_target_status` 는 인메모리. 재시작 시 `idle` 로 되돌아감. 데이터 정합성은 OK (hash idempotent + failed 재시도) 이지만 UX 어색.
- **10K 규모 검증** — 디스크 ~3.5GB, RAM 피크 ~2.4GB, 첫 싱크 2–3시간 예상. 실측 필요.
