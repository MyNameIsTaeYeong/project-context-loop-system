# Confluence MCP 3-scope 싱크 UI 구현

- 일시: 2026-04-24
- 브랜치: `claude/plan-document-sync-ui-aOBNT`
- 관련 결정: D-043 (백엔드, 2026-04-23)
- 관련 이슈: I-030 (해결)

## 배경

전일(2026-04-23) D-043 으로 Confluence MCP 3-scope 싱크 백엔드(REST API 7종 + 동시성 + 안전 속성)가 완성됐으나 UI 가 미구현이었다(I-030). 이번 세션은 그 백엔드를 사용자에게 노출하는 단일 페이지 UI 를 추가한다.

## 결정 사항

- **단일 파일 확장**: `web/templates/confluence_mcp.html` 한 곳에 신규 `syncTargetsPanel()` Alpine 컴포넌트 추가. 신규 페이지·라우트·JS 파일 분리하지 않음 — 기존 페이지 컨벤션과 일치.
- **구 UI 보존**: 단발성 임포트(3탭, `mcpBrowser()`)는 `<details>` 로 접어 "고급" 영역에 유지. I-030 명시 지침("구 UI 유지").
- **다이얼로그 = `<dialog>` + Alpine `:open`**: `closeDialog()`/`confirmXxx()` 만으로 모든 모달 흐름 제어. 외부 라이브러리 도입 없이 Pico 변수와 함께 자연스럽게 어우러진다.
- **자가 시작·정지 폴링**: `setInterval` 을 running/queued 가 하나라도 있을 때만 켜고, 한 번 폴링한 결과 모두가 종료 상태이면 즉시 끈다. 빈 페이지에서 무한 폴링이 돌지 않는다.

## 변경 파일

| 파일 | 변경 |
|---|---|
| `web/templates/confluence_mcp.html` | `syncTargetsPanel()` 컴포넌트 + 검색/대상카드/3개 다이얼로그 마크업 + scoped CSS. 기존 `mcpBrowser()` UI 는 `<details>` 로 감쌈. |
| `.context/ISSUES.md` | I-030 해결됨 섹션으로 이동, I-010 진행 상태에 한 줄 추가. |

## UI 구조

```
[MCP Server Connection] (기존 그대로)
[📚 문서 싱크] ← 신규
  검색 박스
  └ Spaces 섹션 (검색결과) — 카드당 [🏢 공간 전체 싱크]
  └ Pages 섹션  (검색결과) — 카드당 [📄 페이지만] [🌿 하위 포함]
  ──────────
  [🔁 등록된 싱크 대상]
  └ 카드: scope 뱃지 + name + last_sync 상대시간
          + 상태 뱃지(idle/queued/running/completed/failed)
          + 증분 요약 monospace (+N · ~N · -N)
          + [🔄 재싱크] [❌ 해제]
  ──────────
  <dialog> 3종: subtree(즉시 안내) / space(estimate 선행) / unregister(cascade 경고)

[고급: 단발성 임포트] (`<details>` 접힘) ← 기존 mcpBrowser()
```

## 데이터 흐름

| 사용자 동작 | API 호출 | 후속 |
|---|---|---|
| 검색 | `GET /api/confluence-mcp/search?q=...` | `spaces[]`/`pages[]`/`total_pages` 분리 렌더 |
| 📄 등록 | `POST /sync-targets {scope:"page", page_id}` | 토스트 + `loadTargets()` |
| 🌿 등록 (확인 후) | `POST /sync-targets {scope:"subtree", page_id}` | 동일 |
| 🏢 등록 (estimate→확인 후) | `GET /spaces/{key}/estimate` → `POST /sync-targets {scope:"space", space_key}` | 동일 |
| 🔄 재싱크 | `POST /sync-targets/{id}/sync` (409 → 토스트) | `loadTargets()` |
| ❌ 해제 | `DELETE /sync-targets/{id}` | `loadTargets()` + 삭제된 문서 수 토스트 |
| 폴링 | `GET /sync-targets/{id}` × running 대상 | 부분 갱신, 모두 종료 시 자동 정지 |

## 안전·UX 디테일

- **summary 폴백**: 라이브 status 가 없을 때(서버 재시작 후 첫 로드 등)에도 `last_result_json` 을 파싱해 마지막 결과를 표시.
- **상대시간 포맷**: `last_sync_at` 은 SQLite `CURRENT_TIMESTAMP` (UTC, "YYYY-MM-DD HH:MM:SS") 형식이라 `T` + `Z` 보정 후 `Date` 로 파싱.
- **scope 색상**: page=info-blue, subtree=success-green, space=accent-purple — 카테고리 기존 뱃지 팔레트와 톤 일치.
- **재싱크 비활성화**: running/queued 중에는 버튼 disabled — 어차피 백엔드가 409 를 던지지만 UI 단계에서 차단해 클릭 노이즈 제거.
- **폴링 종료 보장**: `_pollOnce()` 마지막에 `_maybePoll()` 재평가로 다음 틱 stale 호출이 누적되지 않음.

## 테스트

- `tests/test_web/test_confluence_mcp_sync_api.py` 19건 모두 통과 — UI 변경은 백엔드 계약을 따르므로 충분.
- 템플릿 자체는 `connected=True/False` 두 분기 모두 Jinja2 렌더 성공 + HTML 태그(article/dialog/details/script/style/div) 균형 검증.
- 기존 `tests/test_web/` 의 14건 실패는 사전 Jinja2 LRUCache 환경 문제로 회귀 아님(2026-04-23 노트 명시).

## 한계 / 다음 작업

- **E2E 자동화 없음**: Playwright 등 도입은 별도 스코프. 현재는 수동 검증(① 등록 ② 폴링 ③ 재싱크 ④ 해제 cascade) 4가지 플로우.
- **SSE 진행률**: 폴링 기반 → 향후 수천 페이지 공간 싱크의 UX 개선 여지. 현재 2초 간격으로 충분.
- **트리 탐색형 진입**: I-010 잔여 — `walk_subtree` 가 준비돼 있어 신규 진입 UX 추가 가능.
