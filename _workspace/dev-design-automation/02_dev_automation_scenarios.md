# 02. 개발 업무 자동화 시나리오

> 기준: `origin/main`. 각 시나리오는 ① 자동화 대상 ② 활용 building block ③ gap(추가 필요분) ④ 워크플로우 ⑤ 산출물 ⑥ 난이도 + 기대효과로 기술.
> 난이도 라벨: **[즉시 PoC]** = 기존 역량 조합만으로 가능 / **[중간]** = 일부 신규 개발 / **[대형]** = 큰 신규 개발.

핵심 전제: **MCP 서버가 이미 동작**하므로 "Claude Code/사내 에이전트가 이 MCP에 붙어 사내지식·코드를 질의"하는 시나리오는 대부분 저비용이다. git_code AST 그래프는 고신뢰라 코드 구조 기반 자동화의 토대가 된다.

---

## D-1. 사내지식 Q&A / Claude Code MCP 연동 [즉시 PoC]
- **대상:** 개발자가 "이 사내 시스템 어떻게 인증하지?", "이 모듈 누가 짰지?"를 IDE에서 즉시 질의
- **building block:** `mcp/server.py run_stdio()` + `search_context`/`get_document`/`get_graph_context`. CLAUDE.md에 이미 클라이언트 설정 예시 존재
- **gap:** 거의 없음. 사내 코퍼스 인덱싱만 선행되면 됨. (선택) 자체 엔드포인트 LLM 라우팅 설정
- **워크플로우:** 코퍼스 인덱싱(git_code+confluence) → MCP 서버 등록 → Claude Code `mcpServers`에 추가 → 자연어 질의
- **산출물:** IDE 내 출처 표시 답변(섹션/문서 링크 포함)
- **기대효과:** 암묵지 탐색 시간 단축. **모든 후속 시나리오의 공통 기반** — 최우선 깔판

## D-2. 코드 리뷰 자동화 [중간]
- **대상:** PR diff에 대해 사내 컨벤션·과거 결정(DECISIONS)·유사 기존 코드를 근거로 리뷰 코멘트 생성
- **building block:** `search_context(include_source_code=True)`로 컨벤션·유사코드 인출, `ast_code_extractor`의 심볼/import 그래프로 변경 함수의 호출/피호출 식별, `get_graph_context`로 영향 반경
- **gap:** ① PR diff 수집 어댑터(GitHub/사내 git) ② diff→질의 변환 + 리뷰 프롬프트 ③ 코멘트 게시(또는 리포트). 평가 하네스로 리뷰 품질 회귀 측정 가능
- **워크플로우:** PR webhook/cron → diff 파싱 → 변경 심볼 추출 → MCP 질의(컨벤션+유사코드+영향) → LLM 리뷰 생성 → PR 코멘트
- **산출물:** 인라인 리뷰 코멘트 + 영향 함수 목록
- **기대효과:** 리뷰 1차 필터 자동화, 컨벤션 준수율↑. 사내지식 근거가 일반 LLM 리뷰 대비 차별점

## D-3. 설계/기술 문서 자동 생성 [중간]
- **대상:** 모듈별 설계 문서·API 레퍼런스·README 초안 생성
- **building block:** git_code 인덱스(심볼+docstring+section_path), `to_graph_data`의 import/contains 그래프, `get_document(format="graph")`, code_doc↔git_code `document_sources` 연결(이미 code_doc/code_summary source_type 존재 → 일부 구현됨)
- **gap:** 모듈 단위 집계·문서 템플릿·생성 트리거. (code_summary 파이프라인이 이미 있어 토대 존재)
- **워크플로우:** 모듈 선택 → 심볼/그래프/docstring 수집 → LLM 문서화 → 마크다운 저장(에디터로 검수)
- **산출물:** 모듈 설계 문서 / API 레퍼런스 마크다운
- **기대효과:** 문서화 부채 해소, 문서-코드 동기화

## D-4. 기술 부채 분석 [중간]
- **대상:** 순환 의존, god-module(과다 fan-in/out), 고립 모듈, TODO/FIXME, 그래프 미연결 영역 탐지
- **building block:** `graph_store.graph` (NetworkX DiGraph) → `nx` 알고리즘(`simple_cycles`, degree 분포), import 그래프, `stats()`
- **gap:** 부채 룰 정의 + 리포트 생성기. TODO/FIXME는 원본 코드 grep 보강
- **워크플로우:** 그래프 로드 → NetworkX 분석(사이클/허브/고립) → 임계치 위반 수집 → 우선순위 리포트
- **산출물:** 기술부채 리포트(모듈별 점수 + 근거 그래프 경로)
- **기대효과:** 리팩터링 우선순위 객관화. git_code 그래프 고신뢰라 **결과 신뢰도 높음**

## D-5. 버그 분류/원인 위치 추정(triage) [중간]
- **대상:** 에러 로그·이슈 텍스트로 관련 모듈·심볼·과거 문서 제시
- **building block:** `search_context`(이슈 텍스트→유사 코드/문서), `get_graph_context`(스택트레이스 심볼 주변 탐색)
- **gap:** 이슈/로그 수집 어댑터, 스택트레이스 파서, 후보 랭킹
- **워크플로우:** 이슈 인입 → 텍스트/스택 추출 → MCP 질의 → 후보 모듈·심볼·관련 PR/문서 제시
- **산출물:** "관련 의심 영역 top-N + 근거" 코멘트
- **기대효과:** 초기 디버깅 진입점 단축

## D-6. 온보딩 자료 자동 생성 [중간]
- **대상:** 신규 입사자용 "이 코드베이스/도메인 핵심" 자료
- **building block:** 그래프 핵심 엔티티(중심성 상위) + 핵심 문서 청크 + `code_summary` + `/api/graph/full` 시각화
- **gap:** 중심성 선별 + 학습경로 순서화 + 자료 템플릿
- **워크플로우:** 그래프 중심성 계산 → 핵심 엔티티/모듈 선정 → 관련 문서/요약 수집 → 온보딩 문서 + 그래프 뷰 조립
- **산출물:** 온보딩 가이드(핵심 모듈 지도 + 용어집 + 추천 학습순서)
- **기대효과:** 온보딩 기간 단축, 신규자 자가학습

## D-7. 변경 영향 테스트 식별 / 테스트 생성 보조 [중간]
- **대상:** 변경 심볼이 영향 주는 테스트·함수 식별, 테스트 스켈레톤 제안
- **building block:** import/contains 그래프 역방향 탐색(`get_neighbors_from_node_id`), `search_context`로 기존 테스트 패턴 인출
- **gap:** 테스트↔대상 매핑(테스트 파일도 git_code로 인덱싱되면 그래프로 연결), 생성 프롬프트
- **워크플로우:** 변경 심볼 → 피호출 그래프 → 연결 테스트 식별 → (선택) 누락 테스트 스켈레톤 생성
- **산출물:** "이 PR이 영향 주는 테스트 목록" + 테스트 초안
- **기대효과:** 회귀 테스트 범위 누락 방지

## D-8. 커밋/PR 메시지·체인지로그 자동화 [즉시 PoC]
- **대상:** diff + 관련 사내 컨텍스트로 Conventional Commits 메시지/PR 설명/체인지로그 생성
- **building block:** `search_context`로 변경 관련 도메인 맥락 보강, diff
- **gap:** diff→프롬프트 래퍼만(경량)
- **워크플로우:** staged diff → 관련 컨텍스트 질의 → 메시지 생성
- **산출물:** 커밋 메시지/PR 본문 초안
- **기대효과:** 작성 시간 절감, 메시지 일관성. **PoC 비용 최소**

## D-9. 시맨틱 코드 검색·예제 추천 [즉시 PoC]
- **대상:** "X 하는 함수 어디 있지/어떻게 쓰지" 자연어 코드 검색
- **building block:** git_code 멀티뷰 임베딩(body=코드, meta=이름+시그니처+docstring) + `search_context(include_source_code=True)`
- **gap:** 거의 없음(이미 git_code 검색 동작). 결과 표시만
- **산출물:** 관련 심볼 + 원본 코드 스니펫
- **기대효과:** 코드 재사용↑, 중복 구현↓

## D-10. API/의존성 마이그레이션 영향 분석 [중간]
- **대상:** "이 라이브러리/내부 API를 바꾸면 어디를 고쳐야 하나"
- **building block:** import 그래프(`imports` relation, `import_symbols`의 `(module, symbol)`) 역방향 질의
- **gap:** 마이그레이션 대상 입력 UI + 영향 목록 집계
- **산출물:** 영향 파일/심볼 체크리스트
- **기대효과:** 마이그레이션 누락 방지

---

## 시나리오 요약 표

| # | 시나리오 | 핵심 building block | 난이도 | 기대효과 |
|---|----------|--------------------|--------|----------|
| D-1 | 사내지식 Q&A/IDE 연동 | MCP search/graph | 즉시 PoC | ★★★ 공통 기반 |
| D-2 | 코드 리뷰 자동화 | search+AST그래프 | 중간 | ★★★ |
| D-3 | 설계/기술문서 생성 | git_code+code_doc | 중간 | ★★ |
| D-4 | 기술부채 분석 | NetworkX 그래프 | 중간 | ★★ (고신뢰) |
| D-5 | 버그 triage | search+graph_context | 중간 | ★★ |
| D-6 | 온보딩 자료 생성 | 그래프 중심성+요약 | 중간 | ★★ |
| D-7 | 변경 영향 테스트 | import 그래프 역탐색 | 중간 | ★★ |
| D-8 | 커밋/PR 메시지 | search_context+diff | 즉시 PoC | ★ |
| D-9 | 시맨틱 코드검색 | git_code 멀티뷰 | 즉시 PoC | ★★ |
| D-10 | 마이그레이션 영향 | import 그래프 | 중간 | ★★ |

**가장 유망 top3:** D-1(공통 기반·최저비용) → D-2(코드 리뷰, 차별화·고효과) → D-4(기술부채, 고신뢰 그래프 활용).
