# 2026-07-13 — Confluence MCP 자동 주기 재싱크 (MCPSyncEngine)

## 배경

"인덱싱된 문서를 주기적으로 재싱크하는 과정이 있는가" 검토 결과:

- 재싱크 커널(`execute_sync_target`)은 완성돼 있으나 **트리거는 대시보드 버튼뿐**.
- 기존 REST `SyncEngine`(`sync/engine.py`)은 주기 루프를 갖췄지만 **어디서도
  기동되지 않는 데드 코드** — 게다가 Phase 2(인덱싱) 없이 메타데이터 임포트만
  수행하고, I-011(사내 REST 차단)로 REST 경로 자체가 사용 불가.
- `config/default.yaml` 의 `sync_interval_minutes` 3종은 어느 코드도 소비하지
  않는 죽은 설정. `claude.md`/`docs/cloud-automation-harness-review.md` 는
  주기 동기화가 있는 것처럼 과대 기술.
- 2026-04-23 세션에서 "자동 주기 실행 — `MCPSyncEngine` + `auto_sync_enabled`
  토글" 로 후순위 명시돼 있었음 → 본 세션에서 그 설계 그대로 구현.

## 구현

1. **`sync/mcp_engine.py::MCPSyncEngine` 신설** (커널/러너 분리 유지)
   - 엔진은 MCP 세션·스토어·임베딩을 직접 알지 않음. `run_target(target_id)`
     콜러블만 주입받아 호출 — 어떤 러너와도 조합 가능.
   - `run_once()`: `list_sync_targets()` 전체를 **순차** 싱크. 순차인 이유:
     대상들이 같은 MCP 서버·임베딩 엔드포인트를 공유하므로 대상 간 병렬화는
     rate limit 만 압박 (대상 내부는 이미 `phase2_concurrency` 로 병렬).
     대상 단위 예외 격리.
   - `start()`/`stop()`: `asyncio.Event` 기반 협조적 종료. sleep 중이면 즉시
     깨어나고, 대상 싱크 진행 중이면 그 대상까지만 마치고 멈춤 — 강제 cancel
     로 워터마크/membership 이 어중간하게 남지 않도록.
   - 첫 사이클은 기동 60초 후 (엔티티 임베딩 사전 구축과 겹침 방지).
2. **웹 러너 공개 승격**: `web/api/confluence_mcp.py`
   `_run_sync_in_background` → `run_sync_in_background`. 자동/수동 경로가
   같은 함수를 통과해 target 단위 락(`_target_locks`)·진행 상태
   (`_target_status`)를 공유 → 같은 대상의 중복 실행은 락에서 걸러짐.
3. **lifespan 연결**: `web/app.py::_build_mcp_sync_engine`.
   `sources.confluence_mcp.{enabled, auto_sync_enabled, server_url}` 3조건
   충족 시에만 생성·start. 종료 시 `await engine.stop()` 후 스토어 close.
   `app.state.mcp_sync_engine` 노출.
4. **설정**: `sources.confluence_mcp.auto_sync_enabled: false` 신규 (기본
   off — 기존 동작 무변경). `sync_interval_minutes: 30` 이 처음으로 실제 소비.
5. **관측성**: `GET /api/confluence-mcp/health` 에
   `auto_sync: {enabled, running, interval_minutes, last_cycle_at}` 추가.

## 테스트

- `tests/test_sync/test_mcp_engine.py` +6: run_once 전체 대상/빈 목록/실패
  격리, start→stop 라이프사이클, 긴 initial delay 중 stop 즉시성, start
  멱등성, 사이클 도중 stop 시 잔여 대상 스킵.
- 싱크 42 passed. 전체 1274 passed / 27 실패는 사전 실패(Jinja2·Starlette
  테스트 픽스처 비호환, clean tree 에서 동일 재현) — 회귀 없음.
- ruff: 신규/변경 파일 클린 (기존 E501 6건 + UP035 1건은 미변경 라인).

## 2차: git_code 자동 주기 싱크 (같은 세션 후속 요청)

MCP 와 동일 패턴을 git_code 소스에 적용하면서 주기 루프를 공용 베이스로 추출.

1. **`sync/periodic.py::PeriodicSyncEngine` 신설** — start/stop/협조적 종료/
   사이클 실패 격리만 아는 범용 루프. 사이클 정의는 `run_cycle` 콜러블 주입
   또는 `run_once` 오버라이드. `MCPSyncEngine` 은 이 베이스의 서브클래스로
   리팩터링 (기존 테스트 7건 무수정 통과 — 공개 동작 불변).
2. **`web/api/git_sync.py::run_sync_in_background` 신설** — 수동 트리거
   (`POST /api/git-sync/start`)와 같은 전역 `_sync_status` 를 guard 로 공유.
   자동·수동 어느 경로든 진행 중이면 상대가 건너뜀. guard 확인과 running
   마킹 사이에 await 가 없어 단일 이벤트 루프에서 이중 실행 불가.
   git 엔진은 서브클래스 없이 `PeriodicSyncEngine(run_cycle, name="git")` 그대로 사용.
3. **`GitSourceConfig.auto_sync_enabled` 필드 + 파싱 추가**,
   `config/default.yaml` 의 `sources.git.auto_sync_enabled: false` 신규.
   `sources.git.sync_interval_minutes: 60` 이 처음으로 실제 소비됨.
4. **lifespan**: `_build_git_sync_engine` — `enabled` + `auto_sync_enabled` +
   `repositories` 3조건 충족 시에만 기동. `app.state.git_sync_engine` 노출.
5. **관측성**: `GET /api/git-sync/status` 에 `auto_sync` 필드 추가.

테스트 +10 (`test_periodic.py` 3, `test_git_auto_sync.py` 7).
전체 1284 passed / 사전 실패 27 동일 — 회귀 없음.

## 남은 것

- UI 토글 — 현재 config 파일로만 on/off. 대시보드 토글은 후순위.
- REST `SyncEngine`(`sync/engine.py`) 데드 코드 정리 여부 결정.
- STATUS 9.9 체크박스 정합 확인 — `git_repository.py::get_changed_files`
  의 git diff 기반 증분 + 변경 없음 조기 종료(`sync_repository`)가 이미
  구현돼 있어 자동 주기 싱크의 무변경 사이클은 저렴. 체크박스만 미갱신으로
  보이므로 다음 세션에서 검증 후 갱신.
