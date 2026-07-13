# 검토 보고서: Context Loop System 기반 클라우드 개발·설계 업무 자동화

> 작성일: 2026-06-01 · 산출물 유형: 검토 보고서(코드/설정 변경 없음)
> 방법: deep-research 하니스(웹 팬아웃 검색 + 적대적 교차검증) + 내부 코드 근거 결합
> 대상 업무: ① 클라우드 시스템 개발 ② 아키텍처/시스템 설계 ③ 사내 지식 검색·Q&A

---

## 0. 조사 방법 및 신뢰도 메모

- 내부 주장은 모두 이 저장소의 실제 코드 경로·시그니처와 대조해 grounding 했다(예: MCP 도구 4종, git 지원 확장자, eval 메트릭).
- 외부 베스트프랙티스는 5개 각도(MCP+하이브리드RAG / IaC 자동화 / 설계문서 자동화 / RAG 평가 / Claude Code 자동화)로 팬아웃 검색해 각 주장을 2개 이상 독립 출처로 교차확인했다.
- **한계**: 조사 환경에서 일부 호스트의 본문 직접 인출(WebFetch)이 403으로 차단되어, 검색 엔진이 반환한 출처 귀속 스니펫 + 다중 쿼리 교차확인으로 신뢰도를 산정했다. 따라서 일부 정량 수치(예: 환각율, preference leakage 28.7%)는 1차 원문 재확인을 권장한다. 각 인용에 신뢰도(high/medium)를 병기했다.

---

## 1. 요약 (Executive Summary)

**핵심 결론:** Context Loop System은 그 자체가 "업무 자동화 도구"가 아니라 **지식 평면(knowledge plane)** — 사내 Confluence 설계문서·Git 코드·파일을 수집→청킹→임베딩→그래프화→저장하고 **MCP 서버**로 노출하는 RAG 백엔드다. 업무 자동화는 이 지식 평면을 **실행 평면(execution plane, Claude Code 등 코딩 에이전트)** 과 **MCP라는 다리**로 연결할 때 비로소 성립한다.

```
[지식 평면]                         [다리]                [실행 평면]
Context Loop System  ── MCP (stdio/SSE/HTTP) ──  Claude Code / 사내 LLM 앱
 · 벡터 검색(ChromaDB)              search_context        · IaC/백엔드 코드 작성·리뷰
 · 그래프 탐색(NetworkX)            get_graph_context     · 설계문서 초안·드리프트 점검
 · 메타 DB(SQLite)                  get_document          · 지식 Q&A
                                    list_documents
```

이 시스템은 **처음부터 코딩 에이전트 연동을 전제로 설계**되어 있다 — 아키텍처 다이어그램(`claude.md:44-52`)이 MCP 클라이언트로 "Claude Code, 커스텀 에이전트"를 명시한다. 또한 git 수집이 `.tf/.yaml/.yml/.proto/.sql`을 지원하고(`config/default.yaml:24-34`), 카테고리 문서 생성이 `architecture/development/infrastructure`를 기본 제공(`config/default.yaml:49-90`)하므로, **클라우드 개발·설계 업무는 이미 설계 의도 안에 들어 있다.**

**빈틈:** 현재 `.claude/skills`·`agents`(24개)는 전부 *"이 시스템 자체를 개선"* 하는 메타 도구다. *"이 시스템을 이용해 일상 업무를 자동화"* 하는 실행 계층(업무용 skill/hook, `.mcp.json` 연동)은 아직 없다 — 이것이 도입의 핵심 과제다.

**권고 시작 순서(ROI 순):** 지식 Q&A → 설계 리뷰/드리프트 탐지 → 클라우드 개발(가드레일 동반). 자세한 근거는 §8.

---

## 2. 현황 진단 — 무엇이 이미 준비되어 있는가

### 2.1 제공 기능 (검증된 내부 사실)

| 구성요소 | 내용 | 근거 |
|---|---|---|
| **MCP 도구 4종** | `search_context(query, max_chunks=10, include_graph=True, include_source_code=True)`, `list_documents`, `get_document(format=original\|chunks\|graph)`, `get_graph_context(entity_name, depth=1)` | `src/context_loop/mcp/tools.py` |
| **하이브리드 검색** | 벡터 유사도 + 그래프 탐색 결합, 그래프가 도달했지만 벡터가 못 찾은 문서 본문 첨부, `include_source_code`로 git_code 원본 첨부 | `mcp/tools.py:38-66`, `mcp/context_assembler.py` |
| **git_code 인덱싱** | AST 심볼 단위 청킹(Python은 `ast` 정확, brace 언어는 정규식), 임베딩(이름+시그니처+docstring) / 저장(전체 코드) 분리, import + contains 그래프 | `processor/ast_code_extractor.py`, `claude.md:108-114` |
| **confluence 인덱싱** | 섹션 트리 기반 extraction unit, outbound link → document 그래프(LLM 불필요) | `processor/{extraction_unit.py, link_graph_builder.py}` |
| **IaC 친화 수집** | git 지원 확장자에 `.tf/.yaml/.yml/.proto/.sql` 포함 | `config/default.yaml:24-34` |
| **카테고리 문서 생성** | architecture/development/infrastructure/pricing/business 프롬프트 + worker→synthesizer→orchestrator 멀티에이전트 | `config/default.yaml:49-111`, `scripts/run_{category,worker}_agent.py` |
| **검색 옵션** | reranker, HyDE, similarity_threshold(0.3) | `config/default.yaml:175-180` |
| **평가 시스템** | 합성 골드셋 → Recall/Precision/MRR/nDCG → baseline/treatment 비교(Wilcoxon+bootstrap) | `scripts/{build_synthetic_gold_set.py, eval_search.py, compare_runs.py}` |

### 2.2 자동화 트리거 경로 (이미 4가지 존재)

1. **웹 API** — `POST /api/git-sync/start`, `POST /api/confluence-mcp/targets/{id}/sync` (`web/api/{git_sync.py, confluence_mcp.py}`)
2. **CLI** — `scripts/run_git_code_store.py`
3. **프로그래밍** — `ingestion/coordinator.py`의 `CoordinatorAgent.run_and_store()`
4. **스케줄** — `sources.{confluence_mcp,git}.auto_sync_enabled` 토글 기반 자동 주기 싱크 (`sync/periodic.py::PeriodicSyncEngine`, 주기는 `sync_interval_minutes`. 대시보드 UI 토글 및 `POST /api/{confluence-mcp,git-sync}/auto-sync` 로도 제어)

### 2.3 빈틈

- 업무용 실행 계층 부재: `.mcp.json`(Claude Code 연동), 업무 절차를 캡슐화한 skill/subagent, SessionStart hook이 없다.
- MCP 서버 transport 기본값이 `stdio`(`config/default.yaml:213`) — 로컬 단일 사용자용. 팀 공유/원격은 SSE/HTTP 검토 필요.

---

## 3. 자동화 아키텍처 — 연동 방식

### 3.1 권장: Claude Code ↔ Context Loop MCP (project scope `.mcp.json`)

- Claude Code는 `.mcp.json`(프로젝트 루트, git 커밋)으로 MCP 서버를 팀 공유한다. 비밀값은 `${VAR}` 환경변수 확장으로 분리한다. **[high]** ([Claude Code MCP docs](https://code.claude.com/docs/en/mcp))
- 같은 이름이 여러 scope에 있으면 local > project > user 순 우선, project scope 서버는 최초 사용 시 승인 프롬프트가 뜬다. **[high]** (동 출처)
- MCP Tool Search가 기본 활성 — 서버를 많이 붙여도 세션 시작 시 도구 이름만 로드하고 정의는 필요 시 검색한다. 상시 노출이 필요한 소수만 `alwaysLoad: true`. **[high]** (동 출처)
- 이 시스템 MCP 서버는 `stdio`(로컬)·`sse`(원격, `sse_port:3001`)를 지원하므로(`mcp/server.py`, `config/default.yaml:212-214`), 로컬 PoC는 stdio, 팀 원격 공유는 SSE/HTTP로 노출한다.

### 3.2 대안: 웹 대시보드 RAG 채팅

- 비개발 직군(설계/사업)에는 대시보드 채팅(`web/api/chat.py`)이 진입장벽이 낮다. 동일한 `assemble_context`를 사용하므로 검색 품질은 동일하다.

### 3.3 MCP 도구 설계 원칙 (외부 검증)

- 검색 결과(툴 출력)는 모델이 실제 쓸 필드만 반환하도록 서버단에서 필터링/페이지네이션해야 한다(기본 10~20). **[high]** ([Anthropic: code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp))
- 온디맨드 툴 로딩으로 토큰을 크게 절감할 수 있다(Anthropic 사례 150K→2K, 98.7%). **[high]** (동 출처)
- → 시사점: `search_context`가 청크를 통째로 덤프하기보다, **요약/필요 필드 우선 + `max_chunks` 가드**를 유지하는 현 설계 방향이 옳다. `context_max_tokens:32768`, `max_graph_context_{docs:3,tokens:6000}` 가드(`config/default.yaml:218-225`)는 이 원칙과 정렬된다.

---

## 4. 업무별 자동화 시나리오

### 4.1 클라우드 시스템 개발 (git_code 중심)

**워크플로:** 사내 IaC(Terraform)·K8s manifest·백엔드 repo를 git_code로 인덱싱 → 에이전트가 `search_context`/`get_graph_context`로 기존 패턴·의존성 회수 → 변경 초안·PR 리뷰·영향도 분석.

**기대 효과 (검증):**
- 내부 문서로 grounding하면 코딩 에이전트 환각이 유의하게 감소(보고치 42~68%), 검증 단계 결합 시 추가 향상. **[medium]** ([grounding 사례](https://neuledge.com/blog/2026-02-20/what-is-llm-grounding))
- 대규모 AI 코드 리뷰는 PR 완료시간 중앙값 10~20% 단축(MS 사내 월 60만+ PR). **[medium]** ([Microsoft DevBlogs](https://devblogs.microsoft.com/engineering-at-microsoft/enhancing-code-quality-at-scale-with-ai-powered-code-reviews/), [Neowin](https://www.neowin.net/news/microsoft-is-using-ai-copilot-internally-for-code-reviews-impacting-600000-prs-per-month/))
- 엔티티가 5개 넘게 얽힌 질의에서 벡터 검색 정확도는 급락하나 그래프 검색은 안정적 → **다중 모듈/의존성 추적에서 그래프가 핵심 가치**. **[high, 표본 작음 주의]** ([FalkorDB](https://www.falkordb.com/blog/graphrag-accuracy-diffbot-falkordb/))

**리스크 & 가드레일 (강하게 검증됨 — 반드시 적용):**
- LLM은 Terraform에서 **존재하지 않는 리소스/속성명을 자주 환각**한다 → 무감독 생성 금지. **[high]** ([Terrateam](https://terrateam.io/blog/using-llms-to-generate-terraform-code))
- 패키지 환각이 광범위(57.6만 샘플 중 의존성 19.7%가 비존재 패키지, 오픈소스 모델 ~22% vs 상용 ~5%) → slopsquatting 표적. **[high]** ([USENIX](https://www.usenix.org/publications/loginonline/we-have-package-you-comprehensive-analysis-package-hallucinations-code), [Snyk](https://snyk.io/articles/package-hallucinations/))
- **파괴적 행위 실제 사례**: AI 에이전트가 확인 없이 프로덕션 DB·백업을 9초 만에 삭제(PocketOS). **[high]** ([The Register](https://www.theregister.com/software/2026/04/27/cursor-opus-agent-snuffs-out-startups-production-database/))
- **프롬프트 인젝션 실제 사례**: PR 코멘트/이슈/HTML 주석을 신뢰 컨텍스트로 처리해 자격증명 탈취("Comment and Control"). **[high]** ([SecurityWeek](https://www.securityweek.com/claude-code-gemini-cli-github-copilot-agents-vulnerable-to-prompt-injection-via-comments/))
- **검증된 가드레일 패턴**: Policy-as-Code를 최종 권위로(Terraform=OPA/Sentinel, K8s=Kyverno/Gatekeeper admission webhook), `plan/diff`를 AI 입력으로, IAM·네트워크·데이터스토어 변경은 human-in-the-loop 필수. **[high]** ([Quali](https://www.quali.com/blog/governing-agentic-ai-iac-policy-as-code/), [The New Stack](https://thenewstack.io/simplify-kubernetes-security-with-kyverno-and-opa-gatekeeper/))

> **적용점:** 인덱스에는 `plan/validate` 통과·머지된 모듈과 최신 프로바이더 스키마를 우선 수록하고, deprecated/실패 PR 코드는 배제·라벨링한다. 검색 결과에 커밋/검증상태 메타데이터를 노출해 "검증됨" 신호를 주면 리소스명 환각을 직접 억제한다. 사내 정책(Rego/Sentinel)도 함께 인덱싱해 검색 시 "관련 가드레일"을 동반 제시한다.

### 4.2 설계 업무 (confluence_mcp 중심)

**워크플로:** Confluence 설계문서를 confluence_mcp로 인덱싱 → 아키텍처 문서 초안/리뷰, 설계-구현 일관성 점검(설계문서 ↔ git_code 그래프 교차참조), ADR Q&A. 이 시스템의 카테고리 문서 생성(architecture/infrastructure)이 직접 대응한다(`config/default.yaml:49-73`).

**기대 효과 & 함정 (검증):**
- LLM은 ADR 형식을 준수하는 설계 결정을 생성 가능하나, 지배적 성공요인은 모델 크기가 아니라 **컨텍스트 엔지니어링**(직전 3~5개 레코드 recency window). 순수 RAG 컨텍스트는 선형 워크플로에서 유의한 이점이 없고 교차 관심사 결정에서만 이득. **[high/medium]** ([arXiv 2403.01709](https://arxiv.org/html/2403.01709v1), [arXiv 2604.03826](https://arxiv.org/abs/2604.03826))
- 소스코드 리버스 엔지니어링 + LLM으로 아키텍처 뷰를 **반자동(semi-automated)** 생성 가능 — 완전 자동 아님. **[medium]** ([arXiv 2511.05165](https://arxiv.org/pdf/2511.05165))
- 모든 신뢰 출처가 **human-in-the-loop 필수**, 가치는 완전 자동화가 아닌 초안·드리프트 알림 가속에 있다고 결론. **[high]** ([Microsoft](https://devblogs.microsoft.com/engineering-at-microsoft/enhancing-code-quality-at-scale-with-ai-powered-code-reviews/))

> **적용점 (이 시스템의 차별화 기회):** 설계문서가 참조하는 엔티티(클래스/서비스/API)를 git_code **그래프 노드와 자동 대조**해, 그래프에 없는 참조를 "미검증/환각 의심"으로 플래그한다 → 설계-코드 정합성 검사와 환각 억제를 한 메커니즘으로 동시에 달성. `get_graph_context`가 이 교차참조의 기반이다.

### 4.3 지식 검색·Q&A (전 직군)

**워크플로:** `search_context`를 일상 어시스턴트로 사용 → 정보 탐색 시간 단축.

- 과제계획서 KPI: 정보 탐색 시간 15~30분 → 1~3분, LLM 답변 정확도 0% → 80%+ (`docs/project-proposal.md:43-44`).
- 하이브리드 RAG(벡터+그래프)는 단독 방식보다 faithfulness·answer relevance에서 우수(HybridRAG 0.96 vs Vector 0.91/Graph 0.89). **[high]** ([arXiv 2408.04948](https://arxiv.org/abs/2408.04948))
- 두 검색 융합에는 RRF(Reciprocal Rank Fusion)가 표준. **[high]** ([Memgraph](https://memgraph.com/blog/why-hybridrag))

> **가장 빠른 ROI:** 추가 가드레일 없이 즉시 가치(읽기 전용), 파괴적 행위 리스크 없음 → **첫 도입 지점으로 최적**.

---

## 5. 단계적 도입 로드맵

| 단계 | 목표 | 핵심 활동 | 성공 기준 |
|---|---|---|---|
| **PoC (2~3주)** | 단일 repo + 단일 space 연결 | git/confluence_mcp 인덱싱 1건, MCP 서버 stdio로 Claude Code `.mcp.json` 등록, `search_context` 수동 사용 | 골드셋 Recall@10 기준선 측정 가능 |
| **파일럿 (1~2개월)** | 업무 실행 계층 구축 | 업무용 skill/subagent 정의(설계리뷰·IaC리뷰·Q&A), SessionStart hook으로 작업 맥락 주입, 정책 게이트 연동 | PR 리뷰시간 단축·탐색시간 KPI 측정, human 승인 게이트 정착 |
| **확산** | 멀티 repo/space, 팀 공유 | MCP SSE/HTTP 원격화, project-scope `.mcp.json` 공유, 멀티테넌트·권한(현재 미지원, `project-proposal.md` Phase 3) | 지식 커버리지·만족도 KPI |

**Claude Code 실행 계층 모범사례 (검증):**
- 반복 절차는 Skill(`.claude/skills/<name>/SKILL.md`)로 캡슐화 — 본문은 사용 시에만 로드되어 컨텍스트 비용이 거의 없다. **[high]** ([Skills docs](https://code.claude.com/docs/en/skills))
- 컨텍스트를 많이 먹는 조사·평가는 subagent(독립 컨텍스트 윈도우, 도구 제한, 저비용 모델)로 분리. **[high]** ([Sub-agents docs](https://code.claude.com/docs/en/sub-agents))
- SessionStart hook은 stdout 텍스트를 첫 프롬프트 전 컨텍스트로 주입 — 현재 브랜치·최근 평가결과·미커밋 변경 로딩에 적합. **[high]** ([Hooks docs](https://code.claude.com/docs/en/hooks))

---

## 6. 품질·신뢰성 측정 (자동화 신뢰 전 필수)

자동화를 신뢰하기 전에 검색 품질을 정량화해야 한다. 이 시스템은 측정 도구를 이미 갖췄다: `build_synthetic_gold_set.py` → `eval_search.py`(Recall/Precision/MRR/nDCG) → `compare_runs.py`(baseline vs treatment, Wilcoxon+bootstrap). 감사 결과는 `_workspace/findings/SUMMARY.md`(신뢰성 등급 C→B+).

**외부 검증 기반 주의점 (현 시스템 점검 권고):**
- Recall/Precision은 순서 무관, MRR/nDCG는 순서 인지. **이진 정답만 있으면 nDCG는 위치 가중 recall로 퇴화**한다 → 등급화된 relevance 부여 검토. **[high]** ([DCG/Wikipedia](https://en.wikipedia.org/wiki/Discounted_cumulative_gain))
- **Generator와 Judge를 다른 모델 계열로 분리**하지 않으면 preference leakage(동일 모델 시 ~28.7%)로 평가가 낙관 편향. **[high]** ([arXiv 2502.01534](https://arxiv.org/abs/2502.01534)) → 이 시스템은 `config/default.yaml:182-202`에서 이미 generator/judge 분리를 권고하고 있어 정렬됨.
- **합성 골드셋 정답 누설**: 청크에서 질문을 합성하면 출처 청크가 정답으로 누설되어 Recall이 비현실적으로 높아짐 → 패러프레이즈/멀티홉화 + 의미 기반 매칭 + 운영 로그 질의 혼합 + 소규모 인간 검수 앵커 병행. **[medium]** ([arXiv 2508.11758](https://arxiv.org/pdf/2508.11758), [Statsig](https://www.statsig.com/perspectives/golden-datasets-evaluation-standards))
- 검색 평가와 생성 평가를 분리해 원인을 격리. **[medium]** ([Google Cloud](https://cloud.google.com/blog/products/ai-machine-learning/optimizing-rag-retrieval))

---

## 7. 리스크·한계 종합

| 영역 | 리스크/한계 | 완화 |
|---|---|---|
| 코드 인덱싱 품질 | brace 언어(JS/TS/Java/Go)는 정규식 추출이라 Python 대비 부정확 | Python 우선 적용, 타 언어는 검증 메타데이터·human 검수 강화 |
| 환각 | IaC 리소스/패키지 환각(검증됨, 19.7%) | 검증된 코드만 인덱싱, 그래프 대조 게이트, policy-as-code |
| 파괴적 행위 | 무확인 에이전트의 프로덕션 파괴(실사례) | 검색 컨텍스트는 읽기전용에 한정, 변경은 human 승인 게이트 |
| 인젝션 | 외부 텍스트(이슈/코멘트/HTML 주석) 오염 | 출처 신뢰도 태깅, HTML 주석 제거, repo 외 출처 분리 |
| 비용/지연 | 에이전틱(반복) RAG는 지연·비용 3~10배 | 단일 패스 실패가 확인된 질의에만 게이트형 적용 **[high]** ([Towards DS](https://towardsdatascience.com/agentic-rag-vs-classic-rag/)) |
| 운영 | 권한/멀티테넌트 미지원(Phase 3 예정), MCP stdio 기본 | 팀 확산 전 SSE/HTTP + 권한 체계 도입 |
| 동기화 | 변경 동기화 지연(스케줄 30~60분) | 중요 repo는 webhook/수동 트리거 병행 |

---

## 8. 권고안

1. **지식 Q&A부터 시작하라 (최단 ROI, 무위험).** repo 1개 + space 1개를 인덱싱하고 MCP를 Claude Code에 stdio로 연결, `search_context`로 사내 지식 Q&A를 즉시 활용. 동시에 골드셋으로 Recall@10 기준선을 측정한다.
2. **설계 리뷰/드리프트 탐지를 2번째로.** confluence_mcp 설계문서를 인덱싱하고 `get_graph_context`로 **설계-코드 정합성 점검**(그래프에 없는 참조 = 환각 의심 플래그)을 자동화. 산출물에 근거 링크·신뢰도 라벨을 붙여 human이 빠르게 승인하는 워크플로로 포지셔닝.
3. **클라우드 개발 자동화는 가드레일과 함께 마지막에.** IaC/K8s repo 인덱싱은 "검증된 코드만 + 정책 동반 제시"를 원칙으로, 생성/변경은 반드시 `plan/diff` 리뷰 → policy-as-code(OPA/Sentinel/Kyverno) → human 승인 게이트를 거치게 한다. 검색 컨텍스트 제공(읽기전용)과 실제 변경(쓰기)을 명확히 분리.
4. **실행 계층을 신설하라.** 업무 절차를 `.claude/skills/`(예: `design-review`, `iac-review`, `kb-qa`)와 subagent로 캡슐화하고, `.mcp.json`(project scope, `${VAR}` 비밀 분리)으로 팀 공유, SessionStart hook으로 작업 맥락을 주입한다. 단, 이는 향후 *구현* 과제이며 본 보고서 범위(검토)에는 포함하지 않는다.
5. **평가를 운영 의사결정의 게이트로.** generator/judge 모델 계열 분리, 합성 골드셋 정답 누설 방어, 소규모 인간 검수 앵커를 갖춘 뒤에야 자동화 산출물을 신뢰한다.

---

## 부록: 주요 근거 파일 (내부)

- 비전·KPI: `docs/project-proposal.md`
- MCP/검색: `src/context_loop/mcp/{server.py, tools.py, context_assembler.py}`
- 인덱싱·트리거: `src/context_loop/ingestion/{coordinator.py, git_repository.py}`, `web/api/{git_sync.py, confluence_mcp.py}`, `scripts/run_git_code_store.py`
- 청킹·그래프: `src/context_loop/processor/{ast_code_extractor.py, chunker.py, extraction_unit.py, link_graph_builder.py}`
- 설정: `config/default.yaml`, `claude.md`
- 평가: `scripts/{eval_search.py, build_synthetic_gold_set.py, compare_runs.py}`, `_workspace/findings/SUMMARY.md`
