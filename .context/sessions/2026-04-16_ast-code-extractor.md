# git_code 처리를 LLM 기반에서 AST 정적 분석으로 전환

- **일시**: 2026-04-16
- **범위**: D-035 이후 — LLM 기반 그래프 추출의 근본적 한계 해결
- **브랜치**: `claude/fix-graph-extractor-timeout-2h6a9`

## 배경: LLM 기반 코드 그래프 추출의 한계

### 문제 발단

D-035에서 git_code를 코드 전용 프롬프트로 graph-only 처리하도록 변경한 이후, `graph_extractor.py`의 map-reduce 청크 처리에서 **빈번한 타임아웃**이 발생했다. 특히 청크가 3개인 경우 2개는 성공하지만 3번째에서 timeout이 발생하는 패턴이 반복되었다.

### 근본 원인 분석

3단계에 걸친 병목을 확인했다:

1. **coordinator.py — 파일 레벨 순차 처리**: `run_and_store()`가 파일을 하나씩 순차적으로 파이프라인에 전달. 파일 N개 × 파일당 M초 = N×M초 소요.
2. **graph_extractor.py — 청크 레벨 순차 처리**: `_extract_map_reduce()`가 `asyncio.gather`를 사용하면서도 내부적으로 순차 대기. 청크 3개 중 마지막이 서버 부하로 인해 timeout.
3. **LLM 서버 prefill 한계**: 코드는 토큰 밀도가 높아(자연어 대비 ~1.5배) 같은 글자 수에서도 prefill 시간이 더 길다. `max_content_chars`를 32K→16K→8K로 줄여도 timeout 재발 — 방향 자체가 잘못됨.

git 히스토리를 추적한 결과, `max_content_chars`를 반복적으로 축소하며 대응한 흔적이 있었으나 매번 revert되었다. **LLM 기반 접근 자체가 코드 처리에 부적합**하다는 결론에 도달했다.

### 왜 코드에 LLM 그래프 추출이 부적합한가

| 관점 | LLM 기반 | AST 기반 |
|------|----------|----------|
| **정확도** | 환각 가능 — 존재하지 않는 관계 생성 | 100% 정확 — 실제 구문 분석 |
| **속도** | 파일당 수초~수십초 (네트워크 + 추론) | 파일당 수 ms (로컬 연산) |
| **비용** | LLM API 호출 비용 | 제로 |
| **안정성** | 서버 부하에 따라 timeout | 결정적(deterministic) |
| **청킹 문제** | 토큰/글자 기반 분할이 함수 중간을 자름 | 구문 구조 기반 분할 |
| **재현성** | 같은 입력에 다른 결과 가능 | 항상 동일한 결과 |

코드는 이미 **구조화된 데이터**다. 함수/클래스/import 관계가 구문에 명시적으로 표현되어 있으므로 LLM이 "추론"할 필요가 없다. 자연어 문서에서 암묵적 관계를 추출하는 것과 근본적으로 다르다.

## 구현: AST 기반 정적 코드 추출기

### 1단계: 병렬화로 임시 완화 (이후 AST 전환으로 대체)

- `graph_extractor.py`: `asyncio.gather` + `Semaphore(4)` + 지수 백오프 재시도
- `coordinator.py`: 파일 레벨 `asyncio.gather` + `Semaphore(3)` 병렬 처리
- `graph_extractor.py`: 코드 전용 `_CODE_MAX_CONTENT_CHARS = 2000`으로 축소

→ 증상 완화에 불과. AST 전환으로 근본 해결.

### 2단계: `ast_code_extractor.py` 신규 모듈 (D-036)

LLM 호출 없이 순수 정적 분석으로 코드 심볼과 import 관계를 추출한다.

**언어별 파서:**
- **Python**: `ast` 모듈 기반 정확한 파싱
- **Go, Java, TypeScript, JavaScript**: 키워드 + 중괄호 매칭 (정규식)
- **기타**: 파일 전체를 단일 심볼로 반환 (fallback)

**핵심 데이터 구조:**
```python
@dataclass
class CodeSymbol:
    name: str           # 심볼 이름
    symbol_type: str    # function, class, method, struct, interface
    signature: str      # 함수/타입 시그니처
    body: str           # 전체 소스 코드
    line_start: int
    line_end: int
    docstring: str = ""
    parent_name: str = ""        # 소속 클래스명 (메서드인 경우)
    parent_signature: str = ""   # 소속 클래스 시그니처
```

**파이프라인 통합:**
```
git clone → store git_code → extract_code_symbols() → to_chunks() + to_graph_data()
                                    ↓                       ↓              ↓
                              CodeExtraction          벡터DB 저장    GraphStore 저장
```

- `pipeline.py`: `source_type == "git_code"`이면 AST 경로로 분기, LLM 호출 완전 우회
- `coordinator.py`: `storage_method_override`를 `"graph"` → `"hybrid"`로 변경 — 벡터 검색도 활성화

### 3단계: 임베딩 대상과 저장 대상 분리

벡터 검색의 정확도를 높이기 위해 **임베딩하는 텍스트**와 **저장/반환하는 텍스트**를 분리했다.

```python
chunks, embed_texts = to_chunks(extraction, title)
embeddings = await embedding_client.aembed_documents(embed_texts)  # 이름+시그니처+docstring
documents = [c.content for c in chunks]                            # 전체 코드
vector_store.add_chunks(chunk_ids, embeddings, documents, metadatas)
```

| 대상 | 내용 | 목적 |
|------|------|------|
| **임베딩 텍스트** | 이름 + 시그니처 + docstring + (부모 클래스) | "VPC 생성 함수"로 검색 시 매칭 |
| **저장 문서** | 헤더 + 전체 소스 코드 | 검색 결과로 코드 원문 반환 |

전체 코드를 임베딩하면 변수명, 문법 키워드 등의 노이즈가 자연어 질의와의 유사도를 희석시킨다.

### 4단계: 메서드 단위 청킹 (D-037)

**문제**: 클래스를 통째로 하나의 청크로 만들면, 메서드 4개짜리 클래스가 하나의 큰 청크가 되어 검색 정밀도가 떨어진다.

**해결**: 클래스 내부 메서드를 개별 청크로 분할하고, 부모 클래스 정보를 메타데이터로 보존한다.

**Python:**
```python
# AS-IS: ClassDef → 단일 심볼
# TO-BE: ClassDef 내부 메서드 → 개별 CodeSymbol(type="method", parent_name="ClassName")
#         메서드 없는 클래스(데이터 클래스 등) → 기존처럼 단일 심볼
```

**Go:**
```go
// 리시버 메서드 → parent_name으로 타입 기록
func (s *VPCService) Create(req CreateRequest) (*VPC, error) { ... }
// → CodeSymbol(name="Create", type="method", parent_name="VPCService")
```

**Java/TypeScript/JavaScript:**
- 클래스 본문을 파싱하여 메서드를 개별 심볼로 추출
- `_extract_class_methods()`로 중괄호 매칭 기반 메서드 경계 감지

**청크 구조 변화:**
```
# AS-IS (클래스 단위)
section_path: "service.py > VPCService"
header: "# File: service.py\n# class: class VPCService"

# TO-BE (메서드 단위)
section_path: "service.py > VPCService > create_vpc"
header: "# File: service.py\n# class VPCService\n# method: def create_vpc(name: str) -> VPC"
```

**그래프 관계 추가:**
- 부모 클래스 엔티티를 중복 없이 추가
- 클래스 → 메서드 `contains` 관계 자동 생성

## 설계 결정

- **D-036**: git_code를 LLM 기반 그래프 추출에서 AST 기반 정적 추출로 전환
- **D-037**: 클래스 단위 → 메서드 단위 청킹 + 부모 클래스 메타데이터

## 테스트 결과

- `test_ast_code_extractor.py`: 33개 전체 통과
  - TestPythonExtraction (10개): 메서드 개별 추출, 부모 클래스 정보, 메서드 없는 클래스 보존
  - TestGoExtraction (6개): 리시버 메서드 parent_name 감지
  - TestTypeScriptExtraction (2개): 클래스 메서드 개별 추출
  - TestJavaScriptExtraction (1개): constructor parent_name 검증
  - TestJavaExtraction (1개)
  - TestFallback (2개)
  - TestToChunks (4개): 메서드 청크 parent 정보 포함 검증
  - TestToGraphData (5개): contains 관계 생성 검증
  - TestEndToEnd (2개): Python/Go 전체 파이프라인
- 기존 테스트: 141개 통과 (5개 실패는 기존 `langchain_core` 의존성 문제 — 변경과 무관)

## 변경 파일

| 파일 | 변경 유형 | 설명 |
|------|---------|------|
| `src/context_loop/processor/ast_code_extractor.py` | 신규 + 수정 | AST 기반 코드 추출기 (689줄) |
| `src/context_loop/processor/pipeline.py` | 수정 | git_code AST 경로 분기 |
| `src/context_loop/processor/graph_extractor.py` | 수정 | 병렬화 + 코드 청크 크기 축소 |
| `src/context_loop/ingestion/coordinator.py` | 수정 | 파일 레벨 병렬 + hybrid 전환 |
| `tests/test_processor/test_ast_code_extractor.py` | 신규 + 수정 | 33개 테스트 |

## 커밋 이력

1. `fix: 순차 처리로 인한 map-reduce 청크 timeout을 병렬 실행+재시도로 해결`
2. `perf: coordinator 파일 레벨 병렬 처리로 파이프라인 처리량 개선`
3. `perf: 코드 그래프 추출 청크 크기를 2000자로 축소`
4. `feat: git_code를 AST 기반 정적 추출로 전환 (LLM 호출 제거)`
5. `refactor: 임베딩 대상과 저장 대상 분리 (검색 정확도 향상)`
6. `feat: 메서드 단위 청킹으로 코드 심볼 추출 세분화`

## 다음 작업

- **Phase 9.9**: 증분 처리 — git diff 기반 변경 파일만 재처리
- **Phase 9.10**: GitHub webhook 기반 자동 동기화
