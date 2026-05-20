# Fix Report — 재인덱싱 FK 위반

## 한 줄 결론

`save_graph_data` 의 신규 노드 INSERT 와 link INSERT 사이 race window 를 제거 +
고아 노드 정리 SQL 의 전역 스캔을 "이번 문서가 unlink 한 노드" 로 좁혀 동시
처리 시 산발적으로 발생하던 `FOREIGN KEY constraint failed` 를 해소.

## 적용 변경

| ID | 파일 | 변경 |
|----|------|------|
| F-FK-01 | `storage/metadata_store.py` | `create_graph_node_with_link` 신규 — graph_nodes + graph_node_documents 두 INSERT 를 한 트랜잭션 한 commit 으로 atomic 처리 |
| F-FK-01 | `storage/graph_store.py` | `save_graph_data` 의 신규 노드 분기가 새 메서드 사용 (기존 `create_graph_node` + `add_node_document_link` 분리 호출 제거) |
| F-FK-02 | `storage/metadata_store.py` | `delete_graph_data_by_document` 의 고아 정리 SQL 을 전역 스캔 → "이번 문서가 unlink 한 노드 ID 집합" 으로 범위 좁힘 |

## race window 제거 메커니즘

**이전 (산발 실패)**
```
[코루틴 A: save_graph_data]
  INSERT graph_nodes  → commit       # node_id=N 생성, link 없음
  ↓ await 양보 ─────────────────────
                                      [코루틴 B: delete_graph_data_by_document]
                                        DELETE WHERE id NOT IN (...)  ← N 까지 삭제
  ↑ ────────────────────────────────
  INSERT graph_node_documents (N, A)  → FK 위반 (graph_nodes.N 없음)
```

**이후 (안전)**
```
[코루틴 A: save_graph_data]
  BEGIN                                # aiosqlite 묵시 transaction
  INSERT graph_nodes                    # node_id=N
  INSERT graph_node_documents (N, A)   # link
  COMMIT                                # 두 INSERT 가 한 번에 가시화
  ↓ 이후 양보
                                      [코루틴 B: delete_graph_data_by_document]
                                        후보 = SELECT node_id WHERE document_id=B
                                        DELETE WHERE id IN 후보 AND link 0 = 0
                                        # N 은 후보 밖이라 영향 없음
```

## 회귀 위험 점검

| 변경 | 잠재 회귀 |
|------|----------|
| `create_graph_node` 단독 호출 폐지 (save_graph_data 한정) | production 에서 호출처 없음 (검색 결과 확인). 테스트만 사용 — 그대로 보존 |
| 고아 정리 범위 축소 | 이전 SQL 은 시스템 전체 고아를 정리했지만, 이번 변경은 이번 문서의 unlink 영향 노드만. 다른 경로의 고아 노드는 정리되지 않을 수 있으나, **production 의 다른 고아 발생 경로는 없음** (모든 노드는 save_graph_data → atomic link, delete 는 cascade) |
| `delete_document` (FK CASCADE) 영향 | 변경 없음 — graph_nodes.document_id 가 owner 문서일 때의 cascade 데이터 손실은 별도 이슈 (이번 fix 범위 외) |

## 테스트

| 파일 | 테스트 | 검증 |
|------|--------|------|
| `tests/test_storage/test_metadata_store.py` | `test_create_graph_node_with_link_atomic` | 새 메서드의 atomic 동작 |
| `tests/test_storage/test_metadata_store.py` | `test_delete_graph_data_by_document_narrow_orphan_cleanup` | 좁힌 고아 정리가 다른 문서 신규 노드 보존 |
| `tests/test_storage/test_graph_store.py` | `test_save_graph_data_concurrent_orphan_cleanup_safe` | asyncio.gather 로 save + 다른 문서 delete 동시 실행, A 노드 5개 모두 보존 |

## 테스트 결과

- `pytest tests/test_storage/ tests/test_processor/` — 350 passed
- `pytest tests/ --ignore=tests/test_eval` — **752 passed** (이전 749 + 신규 3, 회귀 0)
- ruff: regression 0

## 별도 이슈 (이번 fix 범위 외)

- `graph_nodes.document_id` 가 **owner 문서** 만 기록하므로, owner 문서가 cascade 삭제되면 정규 병합 노드가 함께 사라져 다른 문서의 edges 가 silently 손실 — 별도 라운드(graph_nodes 의 document_id FK 를 NULL 허용으로 변경 또는 owner 재할당 로직) 권고
- NetworkX `DiGraph` cross-document multi-edge 손실 — 별도 라운드 (graph-search-diagnosis R2)
