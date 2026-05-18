# 코드 그래프 엔티티 FQN 스코핑과 imports 엣지 유실 수정

- **일시**: 2026-04-17
- **범위**: D-036/D-037 이후 — AST 기반 코드 그래프의 엔티티 병합 충돌 수정
- **브랜치**: `claude/git-sync-context-process-ZPNSe`

## 배경

D-036/D-037로 git_code를 AST 정적 분석 기반으로 전환한 뒤, git sync 실행 시 두 가지 문제가 관찰되었다:

1. **외래키 제약 실패**: `graph_edges.source/target` FK가 `graph_nodes.id`를 찾지 못해 일부 엣지가 누락
2. **그래프 연결성 붕괴**: `contains` 엣지가 엉뚱한 파일의 동일 이름 심볼을 가리키는 현상

원인 추적을 위해 `ast_code_extractor.to_graph_data()` → `graph_store.save_graph_data()` 경로를 따라간 결과, 세 개의 별개 버그를 확인했다.

## 버그 1: imports 엣지 target이 엔티티로 등록되지 않아 유실

### 증상

`from logging import getLogger`, `import datetime` 같은 import에서 생성된 `imports` 관계의 target(`logging`, `datetime`)이 `graph_nodes`에 없어, `save_graph_data`의 `name_to_node_id.get(target)`가 `None`을 반환하고 엣지가 DEBUG 로그 한 줄과 함께 조용히 버려졌다.

### 원인

`to_graph_data()`는 심볼(`function`, `class`, `method`, `struct` 등)만 엔티티로 생성하고, import 대상 모듈은 관계만 생성한 채 엔티티화하지 않았다. `name_to_node_id`는 엔티티 등록 시에만 채워지므로 target 이름이 이름맵에 존재하지 않았다.

### 수정 (commit 87a2784)

`to_graph_data()`에서 `extraction.imports`를 순회하여 각 모듈을 `entity_type="module"` 엔티티로 선등록. 파일 title과 동일한 이름은 제외하고, 중복은 `seen_imports` set으로 차단.

```python
seen_imports: set[str] = set()
for imp in extraction.imports:
    if imp == file_title or imp in seen_imports:
        continue
    seen_imports.add(imp)
    entities.append(Entity(name=imp, entity_type="module", description=""))
```

### 테스트

- `test_creates_import_module_entities`: import당 module 엔티티 1개 생성
- `test_import_matching_file_title_not_duplicated`: 자기 파일 import는 엔티티화하지 않음

## 버그 2: 동일 이름 심볼의 canonical 병합 충돌

### 증상

여러 파일/클래스에 같은 이름을 가진 메서드(`__init__`, `run`, `create`, `handle` 등)가 전부 단일 canonical 노드로 합쳐져, `ClassA → __init__` contains 엣지와 `ClassB → __init__` contains 엣지가 **같은 `__init__` 노드**를 가리키는 현상이 발생했다.

### 원인

`graph_store.save_graph_data()`는 `(entity_name_lower, entity_type)` 키로 canonical 노드를 병합한다. 이는 자연어 문서에서는 올바른 동작이지만 — "같은 이름의 엔티티는 같은 것" — 코드에서는 `UserService.create()`와 `OrderService.create()`처럼 이름이 겹치는 것이 일반적이다.

`ast_code_extractor`는 엔티티 이름으로 짧은 이름(`create`)만 사용하고 있어, 스코프 정보가 증발했다.

### 수정 (commit a291c46)

엔티티 이름을 **파일 + 부모 클래스**로 스코핑된 FQN으로 변경:

```python
def _symbol_fqn(file_title: str, parent_name: str, name: str) -> str:
    if parent_name:
        return f"{file_title}::{parent_name}.{name}"
    return f"{file_title}::{name}"

def _class_fqn(file_title: str, name: str) -> str:
    return f"{file_title}::{name}"
```

- 심볼 등록 루프가 parent 클래스 루프보다 먼저 실행되도록 순서 변경 → Go의 `struct` 같은 특수 타입이 parent에 의해 `class`로 덮어써지지 않도록 보장
- parent 루프는 이미 등록된 FQN을 `seen_class_fqns`로 건너뛰어 중복 생성 차단
- `contains` 관계의 source/target 모두 FQN 사용

### 결과

| AS-IS | TO-BE |
|-------|-------|
| `UserService.create` + `OrderService.create` → 병합 | `service.py::UserService.create` ≠ `order.py::OrderService.create` |
| `file_a.py > helper()` + `file_b.py > helper()` → 병합 | `file_a.py::helper` ≠ `file_b.py::helper` |

### 테스트

- `test_same_method_name_in_different_files_has_distinct_fqn`
- `test_same_method_name_in_different_classes_same_file`
- `test_go_struct_and_parent_dedup`: 심볼 루프가 parent보다 먼저 도는지 + Go struct가 dedup되는지 검증
- 기존 테스트 엔티티 이름을 FQN으로 업데이트 (38개 통과)

## 버그 3: FQN 도입으로 get_neighbors 짧은 이름 검색 회귀

### 증상

FQN 도입 이후 `graph_store.get_neighbors("create_user")`처럼 짧은 이름으로 조회하면 결과가 비어버림. 실제 저장된 이름은 `service.py::UserService.create_user`.

`graph_search_planner`는 LLM이 반환한 엔티티 이름으로 탐색을 수행하는데, LLM이 매번 FQN을 기억/생성하기는 어려워 짧은 이름을 반환하는 경우가 많다.

### 수정 (commit 305d810)

`get_neighbors`에 3단 fallback 매칭 추가:

1. **정확 매칭** (기존 동작 유지)
2. **스코프 이름 매칭**: `"::"` 이후 부분으로 매칭 → `"UserService.create_user"`
3. **짧은 이름 매칭**: `.`의 마지막 세그먼트로 매칭 → `"create_user"`

```python
def _extract_short_name(entity_name: str) -> str:
    if "::" not in entity_name:
        return entity_name
    after_scope = entity_name.split("::", 1)[1]
    if "." in after_scope:
        return after_scope.rsplit(".", 1)[-1]
    return after_scope

def _extract_scoped_name(entity_name: str) -> str:
    if "::" not in entity_name:
        return entity_name
    return entity_name.split("::", 1)[1]
```

정확 매칭이 우선하여 기존 사용처에 영향 없음. 여러 후보가 매칭되면 모두 반환하여 LLM이 컨텍스트로 판단하도록 위임.

### 테스트

- `test_extract_short_name`, `test_extract_scoped_name` (단위)
- `test_get_neighbors_short_name_fallback` (통합)
- `test_get_neighbors_exact_match_wins_over_short_name`: 정확 매칭 우선순위 보장
- graph_store 34개 통과

## 설계 결정

- **D-038**: 코드 심볼 엔티티 이름을 파일 범위 FQN으로 스코핑 (`file::Class.method`) + `get_neighbors` 짧은 이름 fallback

## 변경 파일

| 파일 | 변경 유형 | 설명 |
|------|---------|------|
| `src/context_loop/processor/ast_code_extractor.py` | 수정 | import 모듈 엔티티화 + FQN 헬퍼 + 심볼/parent 루프 순서 재배치 |
| `src/context_loop/storage/graph_store.py` | 수정 | `get_neighbors` 3단 fallback + FQN 파서 헬퍼 |
| `tests/test_processor/test_ast_code_extractor.py` | 수정 + 추가 | FQN 기반으로 기존 테스트 업데이트, 신규 5개 추가 (38개) |
| `tests/test_storage/test_graph_store.py` | 추가 | 4개 신규 (34개) |

## 커밋 이력

1. `87a2784` — fix: import 모듈을 엔티티로 등록해 imports 엣지 유실 방지
2. `a291c46` — fix: 코드 심볼 엔티티에 파일 범위 FQN 사용해 canonical 병합 충돌 차단
3. `305d810` — feat: get_neighbors에 FQN→짧은 이름 fallback 매칭 추가

## 남은 과제 / 알려진 제한

- **Java/Kotlin 오버로드**: 동일 이름 + 다른 시그니처의 메서드는 여전히 단일 엔티티로 dedup됨. FQN에 시그니처 해시를 포함하는 추가 확장이 필요할 수 있음.
- **LLM 프롬프트에 노출되는 스키마 요약**: 현재 FQN이 그대로 노출되어 토큰을 더 소모하고 LLM이 짧은 이름으로 응답하도록 유도하는 명시적 가이드는 없음. fallback 매칭으로 커버되지만 프롬프트 최적화 여지 있음.

## 다음 작업

- Phase 9.9: 증분 처리 (git diff 기반 변경 파일만 재처리)
- Phase 9.10: GitHub webhook 기반 자동 동기화
