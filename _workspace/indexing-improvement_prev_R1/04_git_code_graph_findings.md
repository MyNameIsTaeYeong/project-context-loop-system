# Git Code Graph Extraction — Findings

## 요약

- 총 발견 **11건** (Critical 1, High 7, Medium 2, Low 1)
- 가장 시급한 3건:
  - **F-GG-03** 클래스 상속/구현 관계 누락 (`class Foo(Base)`, `extends`, `implements`) — 의존성 추적 큰 손실
  - **F-GG-11** Go imports가 string literal 모두 매칭 → 함수 호출 안의 string도 import로 잘못 추출 (false positive 다수)
  - **F-GG-05** chunks의 section_path(`file > parent > name`)와 graph entity_name(`file::parent.name`)이 다른 포맷 → join 키 부재

## 발견 사항

### F-GG-01 (HIGH): import된 모듈의 종류 구분 부재 (stdlib/third-party/internal)

- **위치**: `src/context_loop/processor/ast_code_extractor.py:222-233`
- **현재 동작**:
  ```python
  for imp in extraction.imports:
      if imp == file_title or imp in seen_imports:
          continue
      seen_imports.add(imp)
      entities.append(Entity(name=imp, entity_type="module", description=""))
  ```
  - 모든 import를 `module` 타입으로 동일 등록
  - `logging` (stdlib), `pandas` (third-party), `myapp.models` (internal) 구분 없음
- **문제**: 검색에서 "이 모듈이 어떤 외부 라이브러리에 의존하는가" 같은 질의에 응답 어렵음. 내부 모듈 종속성과 외부 라이브러리 종속성을 같은 grain으로 다룸
- **개선 방향**:
  - heuristic:
    - 표준 라이브러리 이름 (sys.stdlib_module_names 사용) → `entity_type="stdlib_module"`
    - `.` 포함 + 첫 segment가 자기 프로젝트 prefix이면 internal → `entity_type="internal_module"`
    - 나머지 → `entity_type="third_party_module"` 또는 그냥 `external_module`
  - description에 구분 사유 기록
- **영향 범위**: 코드 의존성 분석/검색
- **심각도**: High | **공수**: M

---

### F-GG-02 (HIGH): `from x import (a, b)`에서 a, b 자체가 그래프에 없음

- **위치**: `src/context_loop/processor/ast_code_extractor.py:265-272` (relations 생성) + `ast_code_extractor.py:358-365` (imports 추출)
- **현재 동작**: `from x import y` → imports에 `x`만 → relation `file imports x`
- **문제**: 실제로 코드가 의존하는 식별자(`y`)가 그래프에 없음. "이 함수가 어디서 import되는가" 추적 불가
- **개선 방향**:
  - imports를 `list[tuple[str, str | None]]`로 (module, symbol)
  - `from x import y` → relation: `file uses y` + `y belongs_to x`
  - 또는 `imports` 관계에 `label`로 import된 symbol 명시
- **영향 범위**: Python/TS/JS 코드 분석. 코드 검색 쿼리 "X가 어디서 사용되는가"
- **심각도**: High | **공수**: M

---

### F-GG-03 (CRITICAL): 클래스 상속/구현 관계 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:200-283` (to_graph_data 전체), `_extract_python:316-354` (Python ClassDef), `_extract_brace_symbols`
- **현재 동작**:
  - Python: `class Foo(Base, Mixin):` → CodeSymbol의 signature는 `"class Foo"` (line 317) — base 누락
  - Java/TS/JS: 정의 패턴이 `class\s+(\w+)`만 잡음 — `extends`/`implements` 무시
  - to_graph_data는 `contains` (메서드)만 추가
- **문제**:
  - "Foo가 어떤 클래스를 상속받는가" 질의 → 그래프에서 답할 수 없음
  - 인터페이스 구현 (Java/TS) 관계 누락
  - 의존성/타입 분석의 핵심 시그널 부재
- **개선 방향**:
  - Python: `_extract_python`에서 `node.bases`, `node.keywords` 추출 → CodeSymbol에 `bases: list[str]` 필드
  - Brace: 정규식에 `(?:\s+extends\s+([\w<>,\s.]+))?(?:\s+implements\s+([\w<>,\s.]+))?` 추가
  - to_graph_data에서:
    - `Relation(source=file::Foo, target=base, relation_type="inherits")`
    - `Relation(source=file::Foo, target=interface, relation_type="implements")`
- **영향 범위**: OOP 코드의 모든 그래프 추론
- **심각도**: Critical | **공수**: M

---

### F-GG-04 (HIGH): 함수 호출(call graph) 관계 부재

- **위치**: `src/context_loop/processor/ast_code_extractor.py:200-283`
- **현재 동작**: contains/imports만 그래프에 들어감. 함수 본문의 호출 관계 추출 없음
- **문제**: "Foo가 어떤 함수를 호출하는가" 같은 핵심 질의 불가. 검색에서 데이터 흐름 추적 불가
- **개선 방향**:
  - Python: `_extract_python`에서 함수 body의 `ast.walk(node)` → `ast.Call`의 `func.id` / `func.attr` 추출
  - 같은 파일 내 정의된 심볼이면 `Relation(source=file::caller, target=file::callee, relation_type="calls")`
  - 외부 호출은 너무 많아 노이즈 — internal-only로 제한 (또는 known imports만)
  - Brace-language: 정규식으로 어림짐작 (예: `\b(\w+)\s*\(`로 후보 추출 후 알려진 심볼과 매칭)
- **영향 범위**: 코드 분석의 핵심 시그널
- **심각도**: High | **공수**: L (8h+ — 신중한 휴리스틱 + 테스트)

---

### F-GG-05 (HIGH): chunks의 section_path와 graph entity_name 포맷 불일치 → join 키 부재

- **위치**: `src/context_loop/processor/ast_code_extractor.py:158, 161` (to_chunks) ↔ `ast_code_extractor.py:241, 255` (to_graph_data)
- **현재 동작**:
  - chunks: `section_path = f"{file_title} > {parent_name} > {name}"` (공백 separator)
  - graph: `entity_name = f"{file_title}::{parent_name}.{name}"` (FQN format)
- **문제**:
  - 검색 결과에서 같은 심볼을 chunk hit과 graph node hit으로 보고할 때 키가 달라 join 불가
  - hybrid 검색에서 같은 심볼의 chunk 점수와 graph 점수를 결합하기 어려움
- **개선 방향**:
  - Chunk metadata에 `fqn` 필드 추가 (graph entity_name과 동일 포맷)
  - 또는 chunk.section_path 포맷을 FQN으로 통일
  - 메타데이터 추가가 안전 (호환성)
- **영향 범위**: hybrid 검색 / 그래프-청크 결합 검색의 정확성
- **심각도**: High | **공수**: M

---

### F-GG-06 (MEDIUM): file_title의 path 구분자 OS 의존성

- **위치**: `src/context_loop/ingestion/git_repository.py:371` (`title=Path(source_id).name`) + `to_graph_data:216` (file_title 그대로 entity name)
- **현재 동작**: title은 `Path(source_id).name` — 파일명만 (path 없음). FQN은 `{filename}::{parent}.{name}`
- **문제**:
  - 다른 디렉토리에 같은 파일명 (`utils.py`, `models.py`)이 있으면 FQN 충돌 → graph_store의 (name, entity_type) 병합으로 잘못 합쳐짐
  - Windows의 `\` vs Unix의 `/` 차이는 `Path(...).name`이 처리하므로 OK
- **개선 방향**:
  - title을 `source_id` (relative path)로 사용 — graph에서 unique
  - 또는 file_title에 relative path를 포함 (예: `src/utils.py`)
  - 또는 entity_name에 relative path 명시적 포함
- **영향 범위**: 동일 파일명을 가진 모듈이 여러 디렉토리에 있는 레포 (매우 흔함)
- **심각도**: Medium → High (충돌 빈도 따라). 실제 영향이 큼
- **공수**: M (storage migration 영향)

---

### F-GG-07 (MEDIUM): _symbol_fqn이 dot 사용, 중첩 nested에서 모호

- **위치**: `src/context_loop/processor/ast_code_extractor.py:185-197`
- **현재 동작**:
  ```python
  def _symbol_fqn(file_title, parent_name, name):
      if parent_name:
          return f"{file_title}::{parent_name}.{name}"
      return f"{file_title}::{name}"
  ```
- **문제**: nested class `class A: class B:` (F-G-04 해결 후) → parent_name="A", name="B"로 들어가지만, B의 메서드 m은 parent_name="B" → FQN `file::B.m`이 되어 A.B와 분리됨 → 같은 file의 다른 클래스로 보임
- **개선 방향**: parent_name을 dotted path로 (`A.B`) 지원 + _symbol_fqn은 그대로 OK
- **영향 범위**: nested class 사용 시 (F-G-04 해결 후 발현)
- **심각도**: Medium (F-G-04 의존) | **공수**: S (F-G-04와 묶음)

---

### F-GG-08 (MEDIUM): module entity description이 별 정보 없음

- **위치**: `src/context_loop/processor/ast_code_extractor.py:217-219`
- **현재 동작**: `description=f"{extraction.language} file, {len(extraction.symbols)} symbols"`
- **문제**: 검색에서 모듈 entity가 hit 되어도 description이 "python file, 12 symbols" — 무엇에 관한 모듈인지 모름
- **개선 방향**: 모듈 docstring (F-G-01의 module symbol과 같이 추출) 또는 파일 첫 N라인의 주석을 description으로 사용
- **영향 범위**: 그래프 검색에서 모듈 노드의 정보성
- **심각도**: Medium | **공수**: S (F-G-01과 연계)

---

### F-GG-09 (HIGH): Go `_extract_brace_imports`이 모든 string literal을 import로 인식 — false positive 대규모

- **위치**: `src/context_loop/processor/ast_code_extractor.py:427`
- **현재 동작**:
  ```python
  "go": re.compile(r'"([^"]+)"'),
  ```
  - 파일 전체의 모든 `"..."` 문자열을 import로 인식
- **문제**:
  - `fmt.Println("hello world")` → "hello world"가 import 모듈로 등록
  - `json.Unmarshal(data, &v)` → 없음 (이건 string 아님)
  - `os.Getenv("HOME")` → "HOME"이 import
  - **심각한 false positive**: Go 파일의 imports가 잘못된 string으로 채워짐
- **재현/근거**: 임의 Go 파일을 `_extract_brace_imports(content, "go")` → string literal 모두 반환
- **개선 방향**:
  - `import\s+(?:"([^"]+)"|\(\s*((?:[^)]+))\s*\))` 패턴으로 import 블록 anchor
  - 또는 `import "x"` / `import ( "x" "y" )` 두 형태 명시적 처리
- **영향 범위**: Go 코드의 imports/그래프 — false positive로 인한 노이즈
- **심각도**: High (Critical에 가까움) | **공수**: S

---

### F-GG-10 (HIGH): TS `import * as X from "y"`의 alias `X` 손실

- **위치**: `src/context_loop/processor/ast_code_extractor.py:429`
- **현재 동작**:
  ```python
  "typescript": re.compile(r"""from\s+['"]([^'"]+)['"]"""),
  ```
  - module path만 추출, alias 손실
- **문제**: 코드 본문의 `X.foo()` 호출이 어떤 모듈에서 왔는지 추적 불가 (F-GG-04 함수 호출 그래프와 연계)
- **개선 방향**:
  - `import\s+(?:(\*\s+as\s+\w+|\w+|\{[^}]+\})\s+)?from\s+['"]([^'"]+)['"]` 패턴
  - alias 정보를 별도 dict로 보관
- **영향 범위**: TS/JS의 별칭 import 추적
- **심각도**: High | **공수**: M

---

### F-GG-11 (CRITICAL): graph_vocabulary에 ast_code 추출 타입 누락 (`method`, `struct`, `interface`)

- **위치**: `src/context_loop/processor/graph_vocabulary.py:38-58`
- **현재 동작**: ENTITY_TYPES에 `function`, `class`만 ast_code source로 등록. ast_code_extractor는 실제로 `method`, `struct`, `interface`, `module` 모두 사용
- **문제**:
  - graph_search_planner가 vocab을 LLM 가이드로 사용할 때 method/struct/interface가 가이드에 없음 → 플래너가 모르는 타입으로 노드를 못 찾음
  - 코드 검색 정확도 손실
- **재현/근거**:
  ```python
  # ast_code_extractor.py:247
  entities.append(Entity(name=fqn, entity_type=sym.symbol_type, ...))  # symbol_type ∈ {function, class, method, struct, interface}
  # graph_vocabulary.py:56-57 — function, class만 정의
  ```
- **개선 방향**: ENTITY_TYPES에 `method`, `struct`, `interface`, `module` 추가
- **영향 범위**: 그래프 검색의 코드 노드 활용도
- **심각도**: Critical (vocab은 검색 플래너의 단일 정보 출처) | **공수**: S — vocab 4줄 추가

---

### F-GG-12 (LOW): RelationType `imports` vs `import` (vocab) 불일치

- **위치**: `src/context_loop/processor/graph_vocabulary.py:88` ↔ `ast_code_extractor.py:269`
- **현재 동작**: vocab `"import"`, 실제 데이터 `"imports"`
- **문제**: 03 보고서 F-CG-13과 동일. 두 보고서에서 동일 발견. 처리 시 중복 제거.
- **개선 방향**: vocab을 `"imports"`로 수정
- **심각도**: Low → Medium (graph_search_planner 영향 검토 후)
- **공수**: S — 1줄 수정

---

## 검토하지 않은 영역

- `graph_store.py` 의 `(name, entity_type)` 병합 규칙이 코드 entity의 FQN과 잘 어울리는가 (별도)
- `embedder.py` 의 코드 임베딩 모델 적합성
- 다국어 코드 (한글 변수명/주석)의 임베딩
- 거대 단일 함수 (>2000 라인) 청크의 임베딩 입력 한계
