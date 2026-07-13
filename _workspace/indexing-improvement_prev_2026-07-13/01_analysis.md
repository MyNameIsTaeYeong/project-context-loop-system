# 청킹 파이프라인 분석 (직접 분석, 256K LLM 컨텍스트 전제)

## 현재 인덱싱 파이프라인 ─ 분할이 일어나는 4 지점

| # | 위치 | 분할 기준 | 목적 | 결과 |
|---|------|----------|------|------|
| 1 | `chunker.chunk_text` / `chunk_extracted_document` | 마크다운 헤딩 + 원자 블록 + 토큰 한도 (`chunk_size=512`, overlap=50) | **임베딩** 입력 단위 | `list[Chunk]` |
| 2 | `extraction_unit.build_extraction_units` | 섹션 트리 응축/분할 (`target_tokens=1500`, `max_tokens=2400`, overlap=200) | **LLM 그래프 추출** 입력 단위 | `list[ExtractionUnit]` |
| 3 | `ast_code_extractor.to_chunks` | AST 심볼 단위(함수/메서드/클래스/모듈) | **코드 임베딩** 단위 (의미적 분할) | `list[Chunk]` |
| 4 | `pipeline.process_document` | source_type 분기에서 위 결과 소비 | 벡터 저장 + 그래프 저장 | DB write |

## 각 분할이 존재하는 진짜 이유

| 분할 | 강제 요인 | 256K 컨텍스트면 사라지는가? |
|------|----------|--------------------------|
| chunker (#1) | 임베딩 모델 토큰 한도(8K 안팎) + 벡터 검색 정밀도 (큰 문서 1 벡터면 부분 매칭 약화) | ❌ **임베딩 한도/검색 정밀도와 무관**. LLM 컨텍스트와 별 문제 |
| extraction_unit (#2) | LLM 컨텍스트 한도 + 응답 max_tokens 예산 + cross-unit 문제 | ✅ **사라진다**. 256K로 거의 모든 Confluence 페이지 1회 호출 가능 |
| AST to_chunks (#3) | "함수 X가 어디 정의?" 같은 식별자 정밀 검색 + meta-view 정확도 | ❌ **의미적 분할**. 토큰 분할이 아니라 코드 구조 분할. LLM 컨텍스트와 무관 |

## LLM 호출 위치 (인덱싱 시)

| 모듈 | 호출 단위 | max_tokens | 호출 빈도 |
|------|----------|-----------|----------|
| `llm_body_extractor.extract_llm_body_graph` | **ExtractionUnit 1개당 1회** (병렬, sem=3) | 32768 | 문서당 N회 (N = unit 수, 보통 1~10) |
| `graph_extractor.py` | 데이터 클래스 전용. LLM 호출 없음 | — | — |
| `body_extractor.py` | 결정론적, LLM 호출 없음 | — | — |
| `link_graph_builder.py` | 결정론적, LLM 호출 없음 | — | — |
| `ast_code_extractor.py` | AST 기반, LLM 호출 없음 | — | — |

**즉, 인덱싱 LLM 호출은 `llm_body_extractor` 단 한 곳, 그리고 그것이 ExtractionUnit 단위로 호출된다.**

## ExtractionUnit 단위 호출의 알려진 문제 (R1 진단 인용)

이전 라운드(`_workspace/indexing-improvement_prev_R1/03_confluence_graph_findings.md`)에서 R2로 분류된 F-CG-04:

> **LLM cross-unit entity 누적**: 같은 entity가 여러 unit에 등장해도 unit별로 별도 호출되어 통합되지 않음. unit A에서 "AuthService"가 "Cache"와 연결되고, unit B에서 "AuthService"가 "TokenStore"와 연결될 때, 두 관계가 별도 호출 결과로만 존재. GraphStore 정규 노드 병합이 entity는 합쳐주지만, LLM 자신이 "이 둘은 동일 시스템"을 인식하지 못한 채 추출하기 때문에 description/관계 풍부도가 부분적임.

## 256K 컨텍스트에서의 환경 점검

- **사내 LLM**: 256K 컨텍스트 (사용자 명시)
- **max_tokens 출력 한도**: 현재 코드 32768 (`llm_body_extractor.py:77`, `graph_search_planner.py:228`, `chat.py:131`)
- **임베딩 모델**: 변경 없음 (8K 토큰 안팎 가정 — 사내 모델 한도는 미상)
- **평균 Confluence 페이지 크기 추정**: 일반 사내 위키 문서 1K~30K 토큰, 거대 문서도 100K 미만이 대부분 → 256K 입력 한도 내에서 1회 호출 가능

## 문서 단위 LLM 호출 전환 시 영향

| 측면 | 영향 | 정량 |
|------|------|------|
| **그래프 추출 품질** | ✅ **+** 같은 entity가 문서 전체에서 1회 통합 추출. cross-section 관계 자연 포착. R1 F-CG-04 자동 해결 | description 풍부도 +, 중복 entity 감소 |
| **LLM 호출 비용** | ✅ **-** 문서당 호출 횟수 N → 1. 입력 토큰 총량은 비슷 (unit overlap 200 토큰 사라져 오히려 ↓) | 호출 수 70~90% ↓ |
| **속도** | ✅ **+** 동시성 sem=3 의존 ↓. 단일 호출이 약간 길어지지만 직렬 unit 호출의 합보다 빠름 | 인덱싱 wall time 30~50% ↓ |
| **검색 정밀도** | **=** 임베딩 청크는 그대로 유지. LLM 호출 단위 변경은 그래프에만 영향. 그래프 노드/엣지가 풍부해져 그래프 탐색 정밀도 + | 변화 미미 ~ + |
| **출력 토큰 한도 위험** | ⚠️ 큰 문서 1회 호출이면 응답 JSON이 32K 출력 한도 압박 가능 | 안전 가드 필요 |
| **JSON 파싱 안정성** | ⚠️ 응답이 길어지면 잘릴 위험 | 가드 + 폴백 필요 |

## chunker / AST to_chunks 는 어떻게?

| 분할 | 권고 |
|------|-----|
| `chunker` (임베딩용) | **유지**. 임베딩 한도(8K)와 검색 정밀도 때문. 단, R3에서 "문서가 임베딩 한도 이하면 1 벡터" 폴백 검토 가능 (별도 라운드) |
| `extraction_unit` (LLM 그래프 추출용) | **사실상 폐기 가능**. 다만 결정론 `body_extractor` 도 ExtractionUnit을 입력으로 받으므로 — body_extractor 입력은 unit 유지, LLM 입력만 문서 단위로 전환 |
| `ast_code_extractor.to_chunks` (코드 임베딩용) | **유지**. AST 의미 분할이지 토큰 분할 아님. 청킹 제거 대상 아님. git_code는 현재 LLM 호출 자체가 없음 |

## 결론

> **문서 단위 LLM 그래프 추출로 전환하는 것이 강하게 정당화됨.**
>
> 청킹 전체를 없애는 것이 아니라, **LLM 호출 단위만 문서 단위로 변경**한다.
> - 임베딩 청크: 유지 (다른 제약과 별개)
> - 결정론 body_extractor의 unit 입력: 유지 (LLM과 무관)
> - **LLM 그래프 추출의 unit 분할 → 문서 단위 1회 호출**: 변경 (이번 라운드 핵심)

## 안전 가드 (구현 필수)

1. **본문 토큰 추정 가드**: 입력 토큰이 모델 한도(예: 200K)를 초과하면 기존 unit 기반 호출로 폴백
2. **응답 잘림 감지**: JSON 파싱 실패 시 자동 폴백
3. **빈 본문 처리**: title만 있는 문서는 LLM 호출 스킵
4. **테스트 호환**: 기존 `extract_llm_body_graph(units, ...)` 시그니처 유지 (deprecated 마킹), 새 함수 추가
