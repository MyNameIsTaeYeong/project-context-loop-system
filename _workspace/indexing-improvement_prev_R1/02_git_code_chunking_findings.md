# Git Code Chunking — Findings

## 요약

- 총 발견 **16건** (Critical 2, High 11, Medium 2, Low 1)
- 가장 시급한 3건:
  - **F-G-01** Python `_extract_python`이 모듈 docstring/상수/top-level 코드를 누락 → 인덱싱 사각지대
  - **F-G-05** Java 함수 패턴이 함수 호출(예: `int x = bar(`)을 함수 정의로 잘못 인식 — false positive 다수
  - **F-G-13** `collect_files`가 `.venv`/`node_modules`/`__pycache__` 같은 vendored 디렉토리를 필터링하지 않음 → 검색 노이즈

## 발견 사항

### F-G-01 (CRITICAL): Python `_extract_python`이 모듈 docstring/상수/top-level 표현을 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:291-367`
- **현재 동작**:
  ```python
  for node in ast.iter_child_nodes(tree):
      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
          ...
      elif isinstance(node, ast.ClassDef):
          ...
      elif isinstance(node, ast.Import):
          ...
      elif isinstance(node, ast.ImportFrom):
          ...
  ```
  - 모듈 docstring (`"""모듈 설명"""`), 모듈 상수 (`MAX_RETRIES = 3`), top-level expression, `if __name__ == "__main__":` 블록, 모듈 레벨 타입 alias 모두 누락
- **문제**:
  - `MAX_RETRIES`, `DEFAULT_CONFIG` 같은 상수가 검색 불가
  - 모듈 docstring이 청크에 없어 "이 모듈이 무엇인가" 질의 실패
  - 사용자가 `from x import MAX_RETRIES`를 하는 코드를 코드 검색해도 정의 위치 못 찾음
- **재현/근거**: `extract_code_symbols("MAX = 10\n\ndef foo(): pass", "x.py")` → symbols에 `foo`만, `MAX` 없음
- **개선 방향**:
  - (1) 모듈 docstring을 별도 `module` 심볼로 추출 (`ast.get_docstring(tree)`)
  - (2) 모듈 레벨 Assign/AnnAssign을 모아 하나의 `module_constants` 심볼로 묶음 (개별 청크가 너무 많아지지 않게)
  - 권장: (1)+(2) 모두
- **영향 범위**: 모든 Python git_code 검색
- **심각도**: Critical | **공수**: M

---

### F-G-02 (HIGH): Python 데코레이터가 body에 포함되지 않음

- **위치**: `src/context_loop/processor/ast_code_extractor.py:305, 328-329`
- **현재 동작**: `body = "".join(lines[node.lineno - 1 : node.end_lineno])` — `node.lineno`는 `def` 라인 (Python 3.8+에서 데코레이터는 lineno에서 제외됨)
- **문제**: `@property`, `@dataclass`, `@app.route("/users")`, `@pytest.fixture` 같은 의미 결정적 데코레이터가 청크 본문에서 누락 → 검색에서 "라우트가 어떤 함수에 매핑되는지" 질의 실패
- **재현/근거**:
  ```python
  @app.route("/users")
  def list_users(): ...
  ```
  → body는 `def list_users(): ...`만 추출, `@app.route("/users")` 누락
- **개선 방향**:
  - `node.decorator_list`가 있으면 첫 decorator의 lineno부터 body 시작
  - signature 문자열에도 `@decorator` 한 줄 prefix 추가
- **영향 범위**: 데코레이터 활용 프로젝트 (Flask/FastAPI/pytest/SQLAlchemy 등) — 사실상 모든 현대 Python 코드
- **심각도**: High | **공수**: S

---

### F-G-03 (HIGH): Python `_python_func_sig`이 *args, **kwargs, keyword-only, defaults 모두 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:370-390`
- **현재 동작**:
  ```python
  for arg in node.args.args:
      name = arg.arg
      if arg.annotation:
          name += f": {ast.unparse(arg.annotation)}"
      parts.append(name)
  ```
  - `node.args.args`만 사용 → `posonlyargs`, `kwonlyargs`, `vararg`, `kwarg`, `defaults` 모두 누락
- **문제**: 시그니처가 부정확. 예: `def foo(a, *args, **kwargs)` → `def foo(a)`. `def bar(*, key: str)` → `def bar()`. 임베딩 입력 텍스트의 의미 정보 손실
- **개선 방향**:
  - `ast.unparse(node.args)` 한 줄로 모든 args 카테고리 처리
  - 또는 `ast.unparse(node)` 시 전체 함수의 첫 라인만 시그니처로 사용
- **영향 범위**: 모든 Python 함수/메서드 시그니처
- **심각도**: High | **공수**: S

---

### F-G-04 (HIGH): Python 중첩 클래스/중첩 함수 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:321-323`
- **현재 동작**:
  ```python
  methods = [
      child for child in ast.iter_child_nodes(node)
      if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
  ]
  ```
  - 클래스 내부의 nested ClassDef를 메서드 필터에서 누락
- **문제**: 데이터 클래스나 enum 안의 nested class, Meta 클래스(Django) 패턴이 인덱싱 안 됨
- **개선 방향**:
  - 메서드 + 중첩 클래스 모두 자식으로 재귀 처리
  - parent_name을 dotted FQN으로 (예: `Outer.Inner`)
- **영향 범위**: Django Meta, dataclasses + Config nested, Pydantic Settings 등
- **심각도**: High | **공수**: M

---

### F-G-05 (CRITICAL): Java 함수 패턴이 함수 호출을 함수 정의로 잘못 인식 가능

- **위치**: `src/context_loop/processor/ast_code_extractor.py:404-413`
- **현재 동작**:
  ```python
  "java": [
      (re.compile(
          r"^\s*(?:public|private|protected|static|final|synchronized|\s)*"
          r"[\w<>\[\],\s]+\s+(\w+)\s*\(",
      ), "function"),
  ],
  ```
  - modifier 키워드 그룹이 0회 이상(`*`)이라 빈 prefix 허용 → `int foo = bar(x);`도 매칭 가능
  - `[\w<>\[\],\s]+`이 `int`를 잡고 `(\w+)` 가 `bar`를 잡음 → `bar`가 메서드로 등록
- **문제**: 메서드 호출 라인을 메서드 정의로 잘못 인식 → 가짜 심볼이 그래프/청크에 들어감. 사용자는 "메서드 본문이 호출 라인 한 줄" 같은 이상한 청크를 봄
- **재현/근거**: `int x = computeSum(a, b);` 한 줄을 함수로 인식
- **개선 방향**:
  - modifier 키워드를 1회 이상 강제 (`+` 사용) — Java 메서드는 거의 항상 public/private/protected 등을 가짐
  - 또는 return type 자리에 단순 식별자/제네릭만 허용 (정규식 강화)
  - 또는 `_extract_class_methods`의 키워드 블랙리스트를 함수 패턴에도 적용
- **영향 범위**: 모든 Java 파일. false positive 심볼 다수
- **심각도**: Critical | **공수**: S

---

### F-G-06 (HIGH): TypeScript/JavaScript 화살표 함수 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:414-422`
- **현재 동작**: `function` 키워드 패턴만 인식 → `const foo = () => {}`, `export const foo = async () => {}` 누락
- **문제**: 현대 TS/JS 코드의 대다수 함수가 화살표 형태 → 인덱싱 사각지대
- **개선 방향**:
  - 패턴 추가:
    ```
    ^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*[^=]+)?\s*=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*=>
    ```
  - body 종료: 화살표 본문이 `{}` 블록이면 brace match, expression이면 `;` 까지
- **영향 범위**: 모든 React/Node.js 프로젝트
- **심각도**: High | **공수**: M

---

### F-G-07 (HIGH): TypeScript에서 type alias / enum / namespace 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:414-418`
- **현재 동작**: `function`, `class`, `interface`만 인식
- **문제**: `type Foo = ...`, `enum Color { ... }`, `namespace X { ... }` 누락 → 도메인 모델/상태 정의 검색 불가
- **개선 방향**:
  - `^(?:export\s+)?type\s+(\w+)\s*=` 패턴 추가 (body는 같은 라인 또는 다음 `;`까지)
  - `^(?:export\s+)?(?:const\s+)?enum\s+(\w+)` 패턴 추가
- **영향 범위**: TypeScript 프로젝트
- **심각도**: High | **공수**: S

---

### F-G-08 (MEDIUM): Brace-language의 멀티라인 시그니처 후속 처리 미흡

- **위치**: `src/context_loop/processor/ast_code_extractor.py:567-572` (`_METHOD_PATTERNS`)
- **현재 동작**: `^\s*(?:public|private|protected|static|readonly|\s)*(?:async\s+)?(\w+)\s*\(` — 패턴은 첫 라인의 `(`까지만 본 후 `_find_matching_brace`로 `{` 찾음
- **문제**: 시그니처가 멀티라인이고 `{`가 다음 라인에 있는 경우 — 작동은 함 (find가 그 다음 `{`를 찾음). 다만 시그니처 추출(`body[:sig_end].strip()`)이 첫 라인 한 줄로 좁혀짐 → 멀티라인 시그니처의 일부만 signature에 들어감
- **개선 방향**: body가 추출되면 그 안에서 첫 `{` 위치까지 전부 시그니처로 (현재도 `body.find("{")` 사용 — 이미 OK) — 재확인하니 OK. **무효 발견 → 제외**
- **심각도**: ~~Medium~~ → 제외

---

### F-G-09 (HIGH): `_extract_class_methods`의 line_start 계산이 복잡하고 1-off 위험

- **위치**: `src/context_loop/processor/ast_code_extractor.py:643-644`
- **현재 동작**:
  ```python
  line_start=class_line_offset + 1 + inner_content[:method_start_in_inner].count("\n") + 1,
  line_end=class_line_offset + 1 + method_end_line,
  ```
  - `class_line_offset` 0-based + `+1`로 1-based + `count("\n")` + `+1`
  - 수식의 의미 추적 어렵고 1-off 위험
- **문제**: line range가 실제 코드 라인과 어긋날 수 있어 후속 분석(예: blame, 위치 표시)에서 잘못된 라인 출력
- **재현/근거**: 단순 테스트 케이스 작성 필요. 기존 테스트가 line 정확도를 보장하는지 확인 필요
- **개선 방향**:
  - 헬퍼 함수로 분리하고 단위 테스트 강화
  - 또는 `lines[class_line_offset:].splitlines()`로 직접 인덱싱
- **영향 범위**: TS/Java/JS의 클래스 메서드 line 표시
- **심각도**: High | **공수**: M

---

### F-G-10 (HIGH): `_extract_brace_symbols`의 used_ranges가 0-based line_idx와 1-based end_line 혼용

- **위치**: `src/context_loop/processor/ast_code_extractor.py:467, 499, 552`
- **현재 동작**:
  ```python
  if any(s <= line_idx < e for s, e in used_ranges):  # line 467
      continue
  ...
  used_ranges.append((line_idx, end_line))  # line 552 — line_idx 0-based, end_line 1-based
  ```
- **문제**: end_line이 1-based (계산: `content[:block_end].count("\n") + 1`)이므로 다음 iteration의 line_idx(0-based)와 비교 시 1-off → 마지막 라인을 다시 매치할 가능성. 또는 첫 라인의 used_range가 (10, 11)이면 10번 라인만 차단, 11번도 메서드의 일부일 수 있음
- **개선 방향**: `used_ranges.append((line_idx, end_line - 1))` 또는 일관되게 0-based로
- **영향 범위**: brace-language 추출에서 인접 라인이 두 번 매치되거나 누락 — 가끔 중복 심볼/누락 심볼 발생
- **심각도**: High | **공수**: S

---

### F-G-11 (HIGH): Python `from x import (a, b)`에서 a, b 이름이 imports에 누락

- **위치**: `src/context_loop/processor/ast_code_extractor.py:358-365`
- **현재 동작**:
  ```python
  elif isinstance(node, ast.ImportFrom):
      if node.module:
          imports.append(node.module)
      elif node.level > 0:
          ...
  ```
  - 모듈명만 imports에 추가, alias.name (실제 import된 이름) 누락
- **문제**: 검색에서 "process_document 함수가 어디서 import되는가" 질의에 imports 그래프가 도움 못 줌. 의존성 추적 제한
- **개선 방향**:
  - `from x import y` → `x.y` 또는 `("x", "y")` 튜플 보관
  - 또는 별도 `import_symbols: list[tuple[str, str]]` 필드 추가
- **영향 범위**: Python 코드의 의존성 분석/검색
- **심각도**: High | **공수**: S

---

### F-G-12 (MEDIUM): `_extract_fallback`이 거대 unknown 파일을 통째로 한 청크로

- **위치**: `src/context_loop/processor/ast_code_extractor.py:725-741`
- **현재 동작**: 파싱 실패 또는 미지원 언어 → 전체 파일이 단일 심볼/청크
- **문제**: 거대 마크다운, .json, 미지원 언어 파일이 단일 거대 청크 → 임베딩 모델 input 한계 초과, 검색 결과 표시 무의미
- **개선 방향**:
  - `_extract_fallback`이 `chunker.chunk_text`를 호출하여 토큰 기반 분할
  - 또는 to_chunks에서 폴백 심볼은 chunk_text로 후처리
- **영향 범위**: 미지원 언어 파일 또는 파싱 실패 파일
- **심각도**: Medium | **공수**: M

---

### F-G-13 (HIGH): `collect_files`이 `.gitignore`를 무시하고 vendored 디렉토리도 포함

- **위치**: `src/context_loop/ingestion/git_repository.py:306-308`
- **현재 동작**:
  ```python
  for abs_path in clone_dir.rglob("*"):
      if abs_path.is_file() and ".git" not in abs_path.parts:
          candidates.append(...)
  ```
  - `.git`만 제외, `.venv`/`venv`/`node_modules`/`__pycache__`/`dist`/`build`/`target` 등은 포함됨
  - `supported_extensions` 필터로 일부 컷되지만, `.venv` 안의 `.py`, `node_modules` 안의 `.js`/`.ts`는 통과
- **문제**:
  - 라이브러리 코드가 인덱스에 들어가 검색 노이즈 증가
  - 인덱싱 시간/공간 낭비
  - 사용자가 자기 코드를 찾을 때 third-party 코드가 hit
- **재현/근거**: `node_modules/react/cjs/react.production.min.js` 같은 파일이 후보에 들어감
- **개선 방향**:
  - 상수 `_DEFAULT_EXCLUDED_DIRS = {".venv", "venv", "node_modules", "__pycache__", "dist", "build", "target", ".tox", ".pytest_cache", ".mypy_cache", "vendor"}`
  - `collect_files`에서 `any(part in _DEFAULT_EXCLUDED_DIRS for part in abs_path.parts)` 시 건너뜀
  - 사용자 product_scopes의 paths가 이미 좁히지만, 자동 탐지(scope_analyzer) 사용 시 노이즈 큼
- **영향 범위**: 자동 탐지된 product 또는 product paths가 광범위한 레포
- **심각도**: High | **공수**: S

---

### F-G-14 (HIGH): `store_git_code`이 매 파일마다 전체 git_code 목록을 list_documents → O(N²)

- **위치**: `src/context_loop/ingestion/git_repository.py:361-365`
- **현재 동작**:
  ```python
  existing_docs = await store.list_documents(source_type="git_code")
  existing = next(
      (d for d in existing_docs if d.get("source_id") == source_id), None,
  )
  ```
  - N개 파일 처리에 N번의 전체 list_documents 호출
- **문제**: 1000 파일 레포 동기화 시 100만 행 스캔 — 대용량 레포에서 동기화가 매우 느림. SQLite 부하 증대
- **개선 방향**:
  - `sync_repository`에서 한 번 list_documents 후 `dict[source_id, doc]` cache
  - cache를 `store_git_code`에 전달 (signature 변경)
  - 또는 `metadata_store`에 `get_document_by_source(source_type, source_id)` 추가 (단건 lookup)
- **영향 범위**: 대용량 레포 (수백~수천 파일) 동기화 성능
- **심각도**: High | **공수**: M

---

### F-G-15 (MEDIUM): `delete_removed_files`도 매 호출마다 list_documents — F-G-14와 동일 패턴

- **위치**: `src/context_loop/ingestion/git_repository.py:417`
- **현재 동작**: 같은 N×N 문제. 보통 deleted 수가 적어 영향 작음
- **개선 방향**: F-G-14 해결 시 같은 cache 사용
- **심각도**: Medium | **공수**: S (F-G-14와 묶어 처리)

---

### F-G-16 (MEDIUM): 바이너리/대용량/파싱 불가 파일이 `read_text` 폴백에서 silently skip — 통계 없음

- **위치**: `src/context_loop/ingestion/git_repository.py:323-327`
- **현재 동작**: `UnicodeDecodeError`/`OSError`만 warning 로깅하고 건너뜀
- **문제**: 사용자가 "왜 이 파일이 검색 안 되나" 디버깅하기 어려움 — SyncResult에 skipped 통계 없음
- **개선 방향**:
  - SyncResult에 `skipped_binary: list[str]`, `skipped_large: list[str]` 필드 추가
  - 또는 logger.warning 대신 result에 누적
- **영향 범위**: 운영/디버깅 편의
- **심각도**: Medium | **공수**: S

---

### F-G-17 (LOW): `_repo_clone_dir`에서 URL 정규화 없음 → 사용자 실수 시 중복 clone

- **위치**: `src/context_loop/ingestion/git_repository.py:429-435`
- **현재 동작**: URL의 마지막 segment만으로 디렉토리명 결정
- **문제**: `https://github.com/org/repo.git` vs `https://github.com/org/repo` 다른 호출이면 같은 디렉토리. 그러나 대소문자/`.git` suffix 차이는 처리됨. 호스트/조직 차이는 처리 안 됨 (org-A/repo vs org-B/repo이 같은 디렉토리 사용 → 충돌)
- **개선 방향**: `hashlib.sha1(repo_url.lower().encode()).hexdigest()[:8]` suffix를 디렉토리명에 추가
- **영향 범위**: 사용자가 동일 이름 다른 org 레포를 동시에 사용할 때
- **심각도**: Low | **공수**: S

---

## 검토하지 않은 영역

- `scope_analyzer.py` 의 product paths 자동 탐지 정확성 (별도 분석 영역)
- `git_config.py` 의 supported_extensions 기본값 적정성
- 대용량 단일 파일 (예: 10K+ 라인 모놀리스 .py)의 분할 정책 (현재 함수/클래스 단위 — OK 일 수 있음)
- 텍스트 인코딩이 utf-8 외(예: cp949, latin-1)인 파일의 처리 — 현재 strict utf-8
- Git submodule 처리
