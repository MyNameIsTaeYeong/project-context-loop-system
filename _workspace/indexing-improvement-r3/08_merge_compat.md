# PR #60 (R2) + PR #61 (R3) 순차 머지 가능성 검토

## 결론을 먼저

> **순차 머지는 가능. 다만 두 번째 PR (R3) 을 rebase 하면서 conflict 4건 해결 필요.** 의미적 충돌은 없으며 두 변경은 직교한다 — R2 는 LLM 본문 그래프 호출 단위, R3 는 임베딩 청크 단위 + 가상 질문 임베딩.

## 두 PR 변경 파일

### PR #60 (R2) — 4개 파일
- `src/context_loop/processor/llm_body_extractor.py`
- `src/context_loop/processor/pipeline.py`  ← 겹침
- `tests/test_processor/test_llm_body_extractor.py`
- `tests/test_processor/test_pipeline.py`  ← 겹침

### PR #61 (R3) — 15개 파일
- `src/context_loop/processor/chunker.py` (신규 함수)
- `src/context_loop/processor/question_generator.py` (신규 파일)
- `src/context_loop/processor/pipeline.py`  ← **겹침**
- `src/context_loop/mcp/context_assembler.py`
- `src/context_loop/storage/vector_store.py`
- `src/context_loop/web/api/documents.py`
- `src/context_loop/web/templates/partials/tab_chunks.html`
- `config/default.yaml`
- `scripts/diagnose_r3_effect.py`
- `tests/test_processor/test_pipeline.py`  ← **겹침**
- `tests/test_processor/test_chunker.py`
- `tests/test_processor/test_question_generator.py`
- `tests/test_mcp/test_context_assembler.py`
- `tests/test_storage/test_vector_store.py`
- `tests/test_web/test_api_documents.py`

**겹치는 파일 2개**: `pipeline.py`, `test_pipeline.py`

## Rebase 시뮬레이션 결과 (실측)

```bash
git checkout -b sim-r2 origin/claude/lucid-nightingale-4c8b30
git rebase sim-r2 origin/claude/r3-multi-vector-doc-indexing
# CONFLICT (content): pipeline.py
# CONFLICT (content): test_pipeline.py
```

총 conflict **4건**:

### Conflict 1 — `pipeline.py:43-54` (import 라인)

**R2 의 변경**: `llm_body_extractor` 에서 `InputTooLargeError` + `extract_llm_body_graph_for_document` 추가 import
```python
from context_loop.processor.llm_body_extractor import (
    InputTooLargeError,
    extract_llm_body_graph,
    extract_llm_body_graph_for_document,
)
```

**R3 의 변경**: `question_generator` import 추가 (자체 `InputTooLargeError` 보유)
```python
from context_loop.processor.llm_body_extractor import extract_llm_body_graph
from context_loop.processor.question_generator import (
    InputTooLargeError as QuestionInputTooLargeError,
    QuestionGenConfig,
    generate_questions_for_document,
)
```

**해결 — 두 import 를 합침**:
```python
from context_loop.processor.llm_body_extractor import (
    InputTooLargeError,
    extract_llm_body_graph,
    extract_llm_body_graph_for_document,
)
from context_loop.processor.question_generator import (
    InputTooLargeError as QuestionInputTooLargeError,
    QuestionGenConfig,
    generate_questions_for_document,
)
```

→ R2 의 `InputTooLargeError` (llm_body_extractor 의 본문 한도용) 와 R3 의 `QuestionInputTooLargeError` (가상 질문 한도용) 가 별개 의미로 공존. 호출부도 각자 자기 것 catch.

### Conflict 2/3/4 — `test_pipeline.py:813-1094` (테스트 영역)

세 영역에서 신규 테스트 추가 위치 + 기존 테스트 patch 변경이 겹침:

- **813-836**: R2 가 추가한 `test_llm_body_extraction_falls_back_to_units_when_oversized` 와 R3 가 추가한 `test_question_indexing_*` 테스트가 같은 위치에 add 됨
- **842-898**: R2 의 `test_assemble_document_body_*` 테스트와 R3 의 신규 테스트 충돌
- **905-1094**: 큰 영역 — R2/R3 양쪽이 파일 끝에 신규 테스트를 append 한 결과 양쪽 영역이 동시에 추가됨

**해결**: 두 PR 의 신규 테스트를 **모두 보존**. R2 의 4건 + R3 의 3건 + 공통 영역 (`test_llm_body_extraction_skipped_when_disabled` 의 `enable_llm_body_extraction=False` 명시화) 은 둘 다 같은 방향이므로 한 번만 적용.

## 의미적 양립성 — 충돌 없음

머지된 상태에서 `process_document` 흐름:

```
extract_confluence(raw_html) → ExtractedDocument
  │
  ▼ R3
chunk_extracted_document_doclevel(extracted, max_tokens=8000)
  │
  ▼ R3 (가상 질문 생성)
generate_questions_for_document(doc_title, extracted, llm_client)
  │
  ▼ R3 (멀티뷰 + 가상 질문 임베딩)
vector_store.add_chunks(body + meta + question)
  │
  ▼ origin/main
build_extraction_units + extract_body_graph (결정론)
  │
  ▼ R2 (문서 단위 LLM 호출)
extract_llm_body_graph_for_document(doc_title, body, llm_client)
  └ InputTooLargeError → extract_llm_body_graph(units, ...) 폴백
```

**R2 와 R3 가 다른 단계에 영향**:
- R2: **LLM 본문 그래프 추출 호출 단위** (extraction_unit N회 → 문서 1회)
- R3: **임베딩 입자도** + **가상 질문 임베딩** + **검색 dedup 정책**

서로 교차하지 않으며 둘 다 작동.

## 권고 순차 머지 절차

### Step 1 — PR #60 (R2) 먼저 머지

PR #60 베이스가 origin/main 이라 깔끔히 머지됨 — conflict 없음:
- GitHub UI 에서 "Merge pull request" 클릭
- 또는 머지 후 `git pull origin main` 으로 로컬 동기화

### Step 2 — PR #61 (R3) 을 rebase

PR #60 머지 후 origin/main 이 갱신되므로 R3 를 그 위에 rebase:

```bash
git fetch origin
git checkout claude/r3-multi-vector-doc-indexing
git rebase origin/main
# Conflict 4건 발생 — 위 분석대로 해결
git add src/context_loop/processor/pipeline.py
git add tests/test_processor/test_pipeline.py
git rebase --continue
# 모든 테스트 통과 확인
python -m pytest tests/test_processor/ tests/test_mcp/ tests/test_web/ tests/test_storage/ -q
# force-push (rebase 했으므로)
git push --force-with-lease
```

또는 GitHub UI 의 "Update branch" 버튼 (rebase 가 아닌 merge commit 추가 — 히스토리 복잡해지지만 conflict 해결은 같음).

### Step 3 — PR #61 머지

rebase 후 conflict 해결 + 테스트 통과면 머지 가능.

## 머지 후 cleanup 권고 (Optional, 별도 PR)

### Cleanup 1 — `InputTooLargeError` 중복 정리

머지 후 두 모듈에 같은 이름의 별개 예외:
- `processor/llm_body_extractor.InputTooLargeError`
- `processor/question_generator.InputTooLargeError` (코드에서는 `QuestionInputTooLargeError` 로 alias)

**통합 옵션**:
- (a) 공용 모듈 `processor/exceptions.py` 생성, 둘 다 거기서 import — 가장 깔끔
- (b) R3 가 R2 의 `InputTooLargeError` 를 직접 사용하도록 변경 — 의존 방향 추가
- (c) 그대로 두기 — 의미가 다른 두 한도 (LLM 본문 추출 입력 vs 가상 질문 생성 입력) 라 분리 정당화 가능

권고: **(a)** — 작은 follow-up PR

### Cleanup 2 — `_workspace/indexing-improvement` vs `_workspace/indexing-improvement-r3`

머지 후 두 라운드 폴더가 공존. 시간 흐름 추적 목적이라 그대로 두어도 됨.

## 위험 평가

| 항목 | 위험도 | 완화 |
|------|-------|------|
| conflict 해결 실수 | 중 | rebase 후 전체 테스트 통과 필수. 1060+ passed 확인 |
| 의미적 충돌 (런타임 동작) | 낮음 | R2/R3 직교 — 다른 파이프라인 단계 영향 |
| 머지 후 회귀 | 낮음 | 두 PR 각자 전체 테스트 통과 + rebase 후 재실행 |
| R3 효과 미미 (이미 진단) | 별도 결정 | doc-level recall 변화 noise 수준 — 머지 후 운영 사용 여부는 별개 결정 |

## 머지 vs 폐기 결정 — 별개 이슈

이전 진단으로 **R3 의 doc-level 효과가 noise 수준** (set 교환 1↔1, MRR +0.0139) 임을 확인했습니다. 머지 가능성과 별개로:

- **머지하면 좋은 점**: 청크 탭 가상 질문 표시 (UX), 출처 라벨 강화, 인덱싱 단순화 (chunker_doclevel)
- **머지하면 비용**: LLM 호출/문서 +1, 임베딩 호출 ×3~4

순차 머지가 기술적으로 가능하다는 것과 R3 머지의 ROI 가 양수인지는 별개입니다. 기술 검토 결과는 위와 같고, ROI 결정은 사용자 몫.

## 한 줄 답

> **순차 머지 가능. R2 → R3 순서로 진행. R3 rebase 시 `pipeline.py` 의 import 1건 + `test_pipeline.py` 의 테스트 추가 3건 conflict 해결 필요. 의미적 충돌은 없으며 머지 후 정상 작동.**
