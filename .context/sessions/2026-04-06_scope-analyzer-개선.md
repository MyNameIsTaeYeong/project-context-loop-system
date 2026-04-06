# scope_analyzer 개선 — 레이어형 구조 대응 및 대규모 레포 타임아웃 해결

- **일시**: 2026-04-06
- **목적**: scope_analyzer.py의 대규모 레포 타임아웃 문제 해결 + 레이어형 디렉토리 구조에서 상품 단위 올바른 식별

---

## 1. 문제

### 1.1 대규모 레포 타임아웃
- 단일 LLM 호출에 전체 디렉토리 트리를 전달하여 토큰 초과 및 타임아웃 발생
- `directories_only` 모드, `max_entries=2000` 추가로도 미해결

### 1.2 레이어형 구조에서 잘못된 상품 식별
- 레포 구조가 `controller/vpc/`, `service/vpc/`, `repository/vpc/` 형태일 때
- LLM이 레이어(controller, service)를 상품으로 식별하는 문제
- 프롬프트 규칙 추가로도 LLM이 일관되게 따르지 않음 → 코드 레벨 감지 필요

### 1.3 다양한 레이어 패턴
- 레이어 디렉토리가 최상위가 아닌 깊은 경로에 존재 (예: `src/main/controller/`)
- 상품 구분이 디렉토리가 아닌 파일명으로 되는 경우 (예: `controller/vpc_controller.py`)

---

## 2. 해결: 2-pass LLM 분석 아키텍처

### 2.1 자동 선택
- 트리 ≤ 300줄 → **단일 호출** (`_analyze_single_pass`)
- 트리 > 300줄 → **2-pass** (`_analyze_two_pass`)

### 2.2 Pass 1 — 영역 식별
- 얕은 트리(depth 2, directories_only) → 소규모 LLM 호출
- 상품 영역(`_AreaInfo`) 리스트 반환

### 2.3 Pass 2 — 영역별 스코프 확정
- 영역별 서브트리(depth 4, max 300) → 병렬 LLM 호출 (semaphore 동시성 제한)
- 각 영역을 `ProductScopeProposal`로 확정
- 오류 시 기본 glob 패턴으로 폴백

---

## 3. 해결: 코드 레벨 레이어 감지 (`_detect_layered_products`)

LLM 프롬프트에 의존하지 않고 디렉토리 구조를 코드 레벨로 분석.

### 3.1 레이어 그룹 탐색
- BFS로 디렉토리를 끝까지 탐색 (깊이 제한 없음)
- `_KNOWN_LAYER_NAMES` (controller, service, repository, model, handler, route 등 30여개)에 해당하는 형제 디렉토리가 2개 이상이면 레이어 그룹으로 판정
- `layer_base` 필드로 레이어 부모 경로 기록 (예: `"src/main"`)

### 3.2 상품 식별 — 방식 1: 디렉토리 기반 (우선)
```
controller/vpc/     ← 2개 이상의 레이어에 "vpc" 존재 → 상품
service/vpc/
repository/vpc/
```
- 2개 이상의 레이어에 동일 이름 서브디렉토리가 존재하면 상품

### 3.3 상품 식별 — 방식 2: 파일명 기반 (폴백)
```
controller/vpc_controller.py     ← 파일명에서 레이어명 제거 → "vpc"
service/vpc_service.py
```
- `_extract_product_from_filename()`: 파일 stem에서 레이어명(단수/복수)을 제거하고 남은 부분이 상품명
- 디렉토리 기반으로 상품을 찾지 못한 경우에만 시도
- `__init__.py`, 숨김 파일 등은 무시

### 3.4 감지 시 흐름
```
_detect_layered_products() → 상품 목록
  ↓ (있으면)
Pass 1 (LLM) 건너뜀
  ↓
Pass 2만 실행 — _collect_subtrees()로 레이어별 서브트리 합산 → LLM 호출
```

---

## 4. `_collect_subtrees` — 레이어별 서브트리 합산

- `root_path`가 직접 존재하면 해당 디렉토리 서브트리 반환
- 존재하지 않으면 `layer_base` 하위 레이어 디렉토리에서 상품명 매칭
  - 디렉토리 기반: `controller/vpc/` → 서브트리
  - 파일명 기반: `controller/vpc_controller.py` → 파일 목록
- 결과 형식: `[controller/vpc]\n서브트리\n\n[service/vpc]\n서브트리`

---

## 5. Qwen3 추론 태그 대응

- 모든 LLM 호출에 `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` 추가
- `extract_json()`에 `<think>...</think>` 태그 제거 안전장치 추가

---

## 6. 미해결 이슈

- **상품 식별 정확도**: 레이어형 감지가 동작하지만 실제 레포에서 여전히 기대와 다른 결과가 나올 수 있음. 실제 레포 구조에 맞춘 추가 튜닝 필요.
- **혼합 구조**: 일부 상품은 디렉토리 기반, 일부는 파일명 기반인 레포에서의 동작 검증 필요.

---

## 7. 변경 파일

| 파일 | 변경 내용 |
|------|----------|
| `src/context_loop/ingestion/scope_analyzer.py` | 2-pass 아키텍처, `_detect_layered_products`, `_extract_product_from_filename`, `_collect_subtrees`, `_AreaInfo.layer_base` |
| `src/context_loop/processor/llm_client.py` | `extract_json()`에 `<think>` 태그 제거 추가 |
| `tests/test_ingestion/test_scope_analyzer.py` | 61개 테스트 (기존 9개 → 61개) |

---

## 8. 테스트 현황

- **61개 전체 통과**
- 주요 테스트 클래스:
  - `TestBuildDirectoryTree` (9): 트리 생성, 필터링, directories_only
  - `TestParseProposals` (4): LLM 응답 파싱
  - `TestParseAreas` (4): Pass 1 응답 파싱
  - `TestParseSingleProposal` (3): Pass 2 응답 파싱
  - `TestProductScopeProposal` (2): config dict 변환
  - `TestScopeAnalysisResult` (2): 결과 요약/변환
  - `TestAnalyzeRepositoryScope` (3): 단일 호출 E2E
  - `TestAnalyzeTwoPass` (5): 2-pass E2E
  - `TestCollectSubtrees` (6): 서브트리 수집 (디렉토리/파일 기반)
  - `TestDetectLayeredProducts` (12): 레이어 감지 (최상위/깊은/파일명 기반)
  - `TestExtractProductFromFilename` (7): 파일명 파싱
  - `TestLayeredTwoPass` (4): 레이어형 2-pass E2E
