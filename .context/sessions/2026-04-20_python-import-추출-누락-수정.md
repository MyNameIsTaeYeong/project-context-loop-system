# Python import 추출 누락 수정 및 import resolve 설계 논의

- **일시**: 2026-04-20
- **범위**: AST 기반 코드 그래프의 Python import 관계 유실 두 건 수정 + import → 실제 파일/클래스 연결 설계 논의
- **브랜치**: `claude/fix-import-extraction-z1PS5`

## 배경

D-036/D-037/D-038을 거쳐 AST 기반 코드 그래프가 안정화된 뒤, 사용자로부터 "git sync로 만들어진 그래프에서 Python import 관계가 모두 보이지 않는다"는 리포트가 올라왔다. `_extract_python()`과 `to_graph_data()`, 그리고 조회 경로를 따라 내려가며 두 건의 별개 버그를 확인했고, 그 과정에서 "디렉토리까지만 연결되고 실제 파일/클래스로는 연결되지 않는" 본질적 설계 한계를 함께 논의했다.

## 버그 1: 상대 import 완전 누락

### 증상

`from . import utils`, `from .. import parent` 같은 상대 import가 imports 리스트에 전혀 들어가지 않음. 패키지 내부 모듈을 많이 사용하는 프로젝트에서 import 엣지의 10~30%가 누락.

### 원인

`ast_code_extractor.py:_extract_python()`의 `ast.ImportFrom` 처리에서 `node.module`만 검사:

```python
elif isinstance(node, ast.ImportFrom):
    if node.module:           # ← node.module이 None이면 통째로 버려짐
        imports.append(node.module)
```

Python AST에서 `from . import utils`는 `node.module=None, node.level=1, node.names=[alias('utils')]`로 표현되므로 `if node.module`에서 탈락.

참고: `from .submodule import foo`는 `node.module='submodule'`이라 기존 코드에서도 추출되지만, level 정보가 버려져 절대 import와 구분되지 않는 잠재 이슈는 이번 수정에서는 건드리지 않음.

### 수정 (commit `6a4b69e`)

```python
elif isinstance(node, ast.ImportFrom):
    if node.module:
        imports.append(node.module)
    elif node.level > 0:
        # from . import x, from .. import y 같은 상대 import
        prefix = "." * node.level
        for alias in node.names:
            imports.append(prefix + alias.name)
```

- `from . import utils` → `".utils"`
- `from .. import parent` → `"..parent"`
- `from . import a, b` → `".a"`, `".b"`

### 테스트

- `test_extracts_relative_imports` — `.`/`..` prefix 포함 이름 추출 검증

## 버그 2: canonical 병합된 모듈 노드가 두 번째 문서의 그래프 조회에서 누락

### 증상

파일 A가 `from project.api.api_service.services import a_service`로 먼저 해당 모듈을 import한 뒤, 파일 B가 동일 모듈을 import하면 파일 B의 그래프 탭에서 `project.api.api_service.services` 노드가 보이지 않음. 실제로 DB에는 노드가 존재하고 엣지도 있는데 조회만 비어 있음.

### 원인

스키마는 canonical 병합을 위해 두 개의 저장소를 사용한다:

- `graph_nodes.document_id` (legacy 컬럼) — **최초 생성자** 문서 ID만 기록
- `graph_node_documents` (링크 테이블) — 노드↔문서 M:N 관계 저장

`save_graph_data()`는 새 노드 생성 시 `graph_nodes.document_id`에 현재 문서를 넣고 `graph_node_documents`에도 링크를 추가하지만, 기존 노드 병합 시에는 `graph_node_documents`에만 링크를 추가한다 (`graph_store.py:179`). 즉 공유 노드의 `graph_nodes.document_id`는 언제나 A의 ID로 고정.

반면 문서별 조회는 링크 테이블을 보지 않고 legacy 컬럼만 사용했다 (`metadata_store.py:269-275`):

```python
SELECT * FROM graph_nodes WHERE document_id = ?
```

→ B의 그래프 탭에서 공유 노드가 누락. 엣지(`B → project.api.api_service.services`)는 B가 직접 만들어 `graph_edges.document_id=B`로 저장되므로 조회에는 잡히지만, source/target 노드 한쪽이 노드 목록에 없어 프론트엔드에서 제대로 렌더되지 않음.

### 수정 (commit `f9845a6`)

`get_graph_nodes_by_document()`를 링크 테이블 INNER JOIN으로 변경:

```sql
SELECT gn.* FROM graph_nodes gn
INNER JOIN graph_node_documents gnd ON gn.id = gnd.node_id
WHERE gnd.document_id = ?
```

`save_graph_data()`는 신규 생성 노드에도 `add_node_document_link()`를 호출하므로 모든 경로가 링크 테이블에 등록된다 (즉 JOIN 기반 쿼리가 완전함).

기존 테스트 `test_graph_nodes_and_edges`는 `create_graph_node`만 직접 호출하고 링크 테이블은 채우지 않아 JOIN 쿼리에서 0건이 반환되므로, 실제 플로우와 일치하도록 `add_node_document_link()` 호출을 테스트에 추가했다.

### 테스트

- `test_merged_node_visible_in_second_doc_graph_query` — doc1, doc2가 동일 모듈을 import할 때 doc2의 노드 조회에도 공유 모듈 노드가 포함되는지 회귀 검증

## 설계 논의: import 대상을 실제 파일/클래스로 resolve 가능한가

### 사용자 문제 제기

수정 후에도 `from project.api.api_service.services import a_service` 같은 import는 그래프에서 **디렉토리 경로**(`project.api.api_service.services`)까지만 보이고, 실제 `a_service`가 어떤 파일/클래스인지로는 연결되지 않는다. RAG 컨텍스트 정확도 관점에서 이 끊김은 치명적.

### 본질적 한계

`from X import Y`에서 `Y`는 정적으로 세 가지 의미 중 하나:

1. `X/__init__.py`에 정의된 클래스/함수/변수
2. `X/Y.py` 서브모듈
3. `X/Y/__init__.py` 서브패키지

단일 파일의 AST만으로는 구분 불가능. 레포 전체의 파일 레이아웃이 있어야 해소된다.

### 가능한 접근: 레포 인덱스 기반 2-pass resolve

git sync는 모든 Python 파일을 `git_code` 문서로 저장하므로 (`source_id` = 레포 상대 경로), 2-pass가 가능하다.

- **Pass 1 (파일별 수집)**: import를 `(module_path, [imported_names])` 튜플로 구조화해 보존. 현재처럼 이름만 뽑아 엔티티화하지 않음.
- **Pass 2 (git sync 종료 시 1회)**:
  1. `dotted_path → source_id` 맵 구축 (`project/api/api_service/services/a_service.py` ↔ `project.api.api_service.services.a_service`)
  2. 각 import `(module, names)`에 대해:
     - `{module}.{name}`이 레포 파일이면 → **파일 노드**로 엣지 (서브모듈 케이스)
     - 아니면 module 파일/`__init__.py`에 심볼 `name`이 FQN 엔티티로 있으면 → **심볼 노드**로 엣지 (클래스/함수 케이스)
     - 둘 다 실패하면 외부 라이브러리로 보고 현재처럼 모듈 경로 노드 유지

### 리트리벌 플로우와의 관계 (사용자 질문 확인)

사용자가 "임베딩 비교로 가장 유사한 그래프 노드를 찾으면 원본 코드를 리턴한다는 말인가?"라고 질문. 실제로는 다음과 같다:

| 저장소 | 임베딩 대상 | 반환 |
|--------|----------|------|
| VectorStore (ChromaDB) | 심볼의 `이름+시그니처+docstring` | 청크 `content` = 전체 원본 코드 |
| GraphStore `_entity_embeddings` | 엔티티 이름만 (FQN) | 노드 이름/타입/유사도 (코드 없음) |

메인 경로는 **VectorStore로 청크를 찾고 그 청크의 content(전체 코드)를 반환**. 그래프 탐색은 병렬로 노드/엣지 컨텍스트를 추가하는 보조 역할. 하나의 심볼은 `section_path`(청크) = `entity_name`(그래프 FQN)으로 동일 식별자를 공유하므로 청크 ↔ 그래프 노드 양방향 조회가 가능.

→ 따라서 import resolve가 "파일 노드/클래스 FQN 노드"로 정확히 이어지면, 그래프 이웃 탐색 결과의 FQN으로 청크를 재조회해 **2-hop 원본 코드 확장**이 가능해진다. 현재는 import target이 디렉토리까지라 이 확장이 끊긴다.

### 결정 보류

구현 규모가 중간(`CodeExtraction.imports` 데이터 구조 변경 + resolver 모듈 신규 + `save_graph_data` 수정 + post-sync 훅 + 테스트)이라 이번 세션 스코프 외로 판단. 별도 phase로 분리하기로 논의만 하고 종료.

## 변경 파일

| 파일 | 변경 유형 | 설명 |
|------|---------|------|
| `src/context_loop/processor/ast_code_extractor.py` | 수정 | `_extract_python()`에서 `node.level > 0`인 `ImportFrom` 처리 추가 |
| `src/context_loop/storage/metadata_store.py` | 수정 | `get_graph_nodes_by_document()`를 `graph_node_documents` JOIN으로 전환 |
| `tests/test_processor/test_ast_code_extractor.py` | 추가 | `test_extracts_relative_imports` |
| `tests/test_storage/test_graph_store.py` | 추가 | `test_merged_node_visible_in_second_doc_graph_query` |
| `tests/test_storage/test_metadata_store.py` | 수정 | `create_graph_node` 직접 호출 테스트에 `add_node_document_link` 추가 (실제 플로우 정합성) |

## 커밋 이력

1. `6a4b69e` — fix: Python 상대 import (from . / from ..) 추출 누락 수정
2. `f9845a6` — fix: 문서별 그래프 조회가 canonical 병합 노드 누락하던 버그 수정

## 남은 과제 / 알려진 제한

- **import → 파일/심볼 resolve (미구현)**: 위 설계 섹션 참고. `from X import Y`에서 `Y`가 디렉토리 경로까지만 연결되고 실제 파일/클래스 FQN으로 resolve되지 않음.
- **조건부 import**: `try/except import`, `if TYPE_CHECKING:`, 함수 내부 지연 import는 여전히 누락. `_extract_python()`이 `ast.iter_child_nodes(tree)`로 최상위만 순회. `ast.walk` 또는 `NodeVisitor` 전환이 필요하지만 이번 스코프 외.
- **문자열 기반 import**: `importlib.import_module("x")`, `__import__("x")`는 런타임 문자열이라 정적 분석으로는 추적 불가 (구조적 한계).
