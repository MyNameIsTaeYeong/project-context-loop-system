# 재인덱싱 FK 위반 — 근본 원인 진단

## 증상
사용자 보고: 문서 재인덱싱 중 외래키(FK) 제약 위반으로 "처리 실패" 케이스가 종종 발생.

## funnel 분석

```
execute_sync_target
  ├─ _sync_subtree / _sync_space
  │     ├─ _import_nodes_and_upsert (Phase 1 임포트, content_hash 변경 감지)
  │     └─ _prune_stale_memberships → delete_document_cascade (시퀀셜)
  │
  └─ _run_processing_phase (Phase 2)
        ├─ asyncio.Semaphore(concurrency=5)  ← 병렬 처리 진입점
        └─ for doc in created/updated/failed:
              process_document
                ├─ start_reprocessing → delete_graph_data_by_document
                │     └─ ★ 고아 노드 정리 SQL (전역 스캔)
                │
                └─ save_graph_data
                      ├─ create_graph_node (await + commit)     ★ link 없는 신규 노드
                      ├─ ← await 양보 시점 ───────────────────  ★
                      └─ add_node_document_link (await + commit)
                                ↑
                                ↑ ← 여기서 graph_nodes.id 가 사라져 FK 위반
```

## 근본 원인 (Critical)

### F-FK-01: `save_graph_data` 의 `create_graph_node` 와 `add_node_document_link` 사이 **race window**

`graph_store.save_graph_data` 의 신규 노드 생성 흐름:

```python
# storage/graph_store.py:194-209 (요지)
node_id = await self._store.create_graph_node(
    document_id=document_id, entity_name=..., entity_type=..., properties=...,
)                                       # ← commit #1: graph_nodes 에 행 추가
                                        # ← 이 시점 graph_node_documents 에는 link 없음
await self._store.add_node_document_link(node_id, document_id)
                                        # ← commit #2: graph_node_documents 에 link 추가
```

`create_graph_node` 와 `add_node_document_link` 가 **별도 commit**. 그 사이 `await` 가 다른 코루틴에 양보하면, **`_run_processing_phase` 가 같은 SQLite connection 으로 동시 처리하는 다른 문서의 `delete_graph_data_by_document` 가 호출되어** `graph_nodes` 의 신규 노드를 **고아 노드로 인식하여 삭제** 한다:

```sql
-- delete_graph_data_by_document (metadata_store.py:507-512)
DELETE FROM graph_nodes WHERE id NOT IN (
    SELECT DISTINCT node_id FROM graph_node_documents
)
```

이 SQL은 **방금 만들어진 신규 노드를 link 등록 직전 시점에 잡아낸다**. 그 직후 `add_node_document_link(node_id=...)` 가 사라진 `node_id` 를 참조하면 **`FOREIGN KEY constraint failed`** 가 발생.

### F-FK-02: 고아 노드 정리 SQL 의 과도한 범위

위 SQL 은 "**이번 문서 처리와 무관한 모든 노드까지 전역 스캔**" 한다. 진짜 의도는 "**이번 unlink 로 link 가 0 이 된 노드만 삭제**" 인데, 실제는 시스템 전역의 모든 노드를 검사한다. 동시 처리 중인 다른 문서의 신규 노드(아직 link 안 됨)도 잘못 제거된다.

### F-FK-03 (보조): `_prune_stale_memberships` ↔ `_run_processing_phase` 분리는 시퀀셜이지만 **process 내부 병렬 처리는 안전 가드 없음**

`_run_processing_phase` 의 `Semaphore(concurrency=5)` 가 같은 connection 의 aiosqlite 위에서 5 개 코루틴을 동시 진행. aiosqlite 는 single connection 이라 transaction 은 직렬화되지만, **여러 commit 사이의 await 양보 시점이 race window** 가 된다.

## FK 위반이 일관되게 발생하지 않는 이유

- 신규 노드 비율(병합되는 정규 노드 vs 진짜 신규)이 매번 다름
- 동시 처리 문서의 entity 중복도가 매번 다름
- `await` 스케줄러의 양보 시점이 nondeterministic
- 그래서 "종종" 보이는 산발적 실패 형태로 나타남

## 영향 범위

- **재인덱싱**: confluence sync 의 Phase 2 (`_run_processing_phase`), 수동 재처리 API 모두 영향
- **데이터 손실 위험**: 신규 노드가 잘못 삭제된 경우, 그 노드와 관련된 edges 도 함께 cascade 삭제 — silent loss
- **메트릭**: 그래프 인덱스가 부분적으로 채워진 상태 → graph 검색 메트릭 추가 손실

## 개선 방향

### 1. (Critical) `create_graph_node` + `add_node_document_link` 원자화
- `INSERT graph_nodes` 와 `INSERT graph_node_documents` 를 **한 트랜잭션 안에서 두 INSERT 후 한 번에 commit** 하도록 변경
- 별도 메서드 `create_graph_node_with_link(document_id, ...)` 신규 — 신규 노드 생성은 무조건 link 와 함께
- 결과: `await` 양보 시점에 graph_nodes 에 행이 있지만 link 가 없는 "고아 윈도우" 가 사라짐

### 2. (High) 고아 노드 정리 SQL 의 범위 좁히기
- 전역 스캔 → 영향받은 노드 한정
- 변경:
  ```sql
  -- 이번 문서 unlink 직전 link 되어 있던 노드 중에서 지금 link 가 0 인 것만 삭제
  DELETE FROM graph_nodes WHERE id IN (
      SELECT id FROM graph_nodes WHERE id IN (?, ?, ...)  -- 이번 문서가 unlink 한 노드들
      AND id NOT IN (SELECT DISTINCT node_id FROM graph_node_documents)
  )
  ```
- 또는 더 간단히: 이번 문서의 `graph_node_documents` 행 삭제 시 그 행에 있던 `node_id` 만 후속 검사

### 3. (Medium) `save_graph_data` 전체를 명시적 트랜잭션으로 감싸기 (선택)
- 더 강한 보장. 단, aiosqlite 의 명시 transaction 사용법과 호환성 확인 필요.
- 1+2 만으로도 race window 해소되므로 본 라운드에서는 1+2 만.

## 예상 회귀 위험

- (1) 의 영향: 신규 노드는 항상 link 동반 — 기존 호출처(`create_graph_node` 단독)가 있다면 영향. 검토 필요.
- (2) 의 영향: 고아 노드가 다른 경로로 발생한 케이스 (예: 마이그레이션) 가 정리되지 않을 수 있음 — 별도 GC 함수가 있는지 확인 필요.

## 확인할 코드 경로 (추가 검토)

- `create_graph_node` 단독 호출 위치: production 코드 vs 테스트
- 다른 고아 노드 정리 경로: `get_orphan_node_ids` 사용처
