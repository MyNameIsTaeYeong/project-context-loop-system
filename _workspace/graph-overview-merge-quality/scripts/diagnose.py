"""GraphStore 엔티티 통합 품질 진단 스크립트.

~/.context-loop/data/metadata.db 를 직접 읽어 다음을 산출한다:
- 인덱스 현황 (노드 수, 엣지 수, type 분포, 문서당 노드 수)
- 잠재 중복 그룹 탐지 (표면 정규화 / 단복수·약어 / 타입 충돌 / FQN)
- 통합 품질 메트릭 (duplication_ratio_surface, type_conflict_count,
  cross_document_node_ratio, orphan_edge_count 등)

재현 가능하도록 결과를 JSON 으로 stdout 에 출력한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

DB_PATH = Path("~/.context-loop/data/metadata.db").expanduser()


def normalize_surface(name: str) -> str:
    """공백/하이픈/언더스코어/마침표 제거 + lower (다국어 보존)."""
    return re.sub(r"[\s_\-.]+", "", name).lower()


def extract_short_name(entity_name: str) -> str:
    """FQN 코드 심볼에서 짧은 이름 추출 — graph_store._extract_short_name 와 동일."""
    if "::" not in entity_name:
        return entity_name
    after_scope = entity_name.split("::", 1)[1]
    if "." in after_scope:
        return after_scope.rsplit(".", 1)[-1]
    return after_scope


def extract_scoped_name(entity_name: str) -> str:
    """FQN 에서 파일 범위 제거."""
    if "::" not in entity_name:
        return entity_name
    return entity_name.split("::", 1)[1]


def levenshtein_similarity(a: str, b: str) -> float:
    """SequenceMatcher 기반 0..1 유사도 (Levenshtein 근사)."""
    return SequenceMatcher(None, a, b).ratio()


def main() -> None:
    if not DB_PATH.exists():
        print(json.dumps({"error": f"DB not found: {DB_PATH}"}, ensure_ascii=False))
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 스키마 변형 확인
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'",
    ).fetchall()}
    has_link_table = "graph_node_documents" in tables

    # 노드 로드
    nodes = [dict(r) for r in cur.execute(
        "SELECT id, document_id, entity_name, entity_type, properties FROM graph_nodes",
    ).fetchall()]

    # 엣지 로드
    edges = [dict(r) for r in cur.execute(
        "SELECT id, document_id, source_node_id, target_node_id, relation_type FROM graph_edges",
    ).fetchall()]

    # 노드 ↔ 문서 연결: 신스키마(graph_node_documents) 우선, 없으면 legacy(graph_nodes.document_id)
    node_doc_links: dict[int, set[int]] = defaultdict(set)
    if has_link_table:
        for r in cur.execute(
            "SELECT node_id, document_id FROM graph_node_documents",
        ).fetchall():
            node_doc_links[r["node_id"]].add(r["document_id"])
    for n in nodes:
        if not node_doc_links[n["id"]] and n["document_id"]:
            node_doc_links[n["id"]].add(n["document_id"])

    # 통계 계산
    doc_count = len({r[0] for r in cur.execute(
        "SELECT id FROM documents",
    ).fetchall()})
    type_counter: dict[str, int] = defaultdict(int)
    for n in nodes:
        type_counter[n["entity_type"] or "(null)"] += 1

    docs_with_nodes: dict[int, int] = defaultdict(int)
    for nid, dids in node_doc_links.items():
        for d in dids:
            docs_with_nodes[d] += 1
    avg_nodes_per_doc = (
        sum(docs_with_nodes.values()) / len(docs_with_nodes)
        if docs_with_nodes else 0
    )

    # === 1. 표면 정규화 기반 중복 ===
    surface_groups: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        key = normalize_surface(n["entity_name"])
        surface_groups[key].append(n)
    surface_dup_groups = {
        k: v for k, v in surface_groups.items() if len(v) >= 2
    }
    surface_dup_nodes = sum(len(v) for v in surface_dup_groups.values())

    # === 2. 표면 정규화 + 동일 type ===
    surface_same_type_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for n in nodes:
        key = (normalize_surface(n["entity_name"]), n["entity_type"] or "")
        surface_same_type_groups[key].append(n)
    surface_same_type_dup = {
        k: v for k, v in surface_same_type_groups.items() if len(v) >= 2
    }

    # === 3. 타입 충돌: 동일 lower(name) 다른 type ===
    by_lower: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_lower[(n["entity_name"] or "").lower()].append(n)
    type_conflict_groups: dict[str, list[dict]] = {}
    for k, v in by_lower.items():
        types = {x["entity_type"] for x in v}
        if len(types) >= 2:
            type_conflict_groups[k] = v

    # === 4. 표면 정규화 후 동일하지만 다른 type ===
    type_conflict_after_surface: dict[str, list[dict]] = {}
    for k, v in surface_groups.items():
        types = {x["entity_type"] for x in v}
        if len(v) >= 2 and len(types) >= 2:
            type_conflict_after_surface[k] = v

    # === 5. 단/복수, 약어 후보 — Levenshtein 0.85 + 동일 type ===
    fuzzy_pairs: list[dict] = []
    n_list = nodes
    seen_pairs = set()
    for i in range(len(n_list)):
        for j in range(i + 1, len(n_list)):
            a, b = n_list[i], n_list[j]
            if a["entity_type"] != b["entity_type"]:
                continue
            na = normalize_surface(a["entity_name"])
            nb = normalize_surface(b["entity_name"])
            if na == nb:
                continue  # 이미 surface_groups 에서 잡힘
            if not na or not nb:
                continue
            sim = levenshtein_similarity(na, nb)
            if sim >= 0.85:
                key = tuple(sorted([a["id"], b["id"]]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                fuzzy_pairs.append({
                    "a": {"id": a["id"], "name": a["entity_name"],
                          "type": a["entity_type"]},
                    "b": {"id": b["id"], "name": b["entity_name"],
                          "type": b["entity_type"]},
                    "similarity": round(sim, 3),
                })

    # === 6. FQN 처리 — 같은 short_name 을 가진 코드 심볼들 ===
    fqn_short_groups: dict[str, list[dict]] = defaultdict(list)
    fqn_scoped_groups: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        name = n["entity_name"] or ""
        if "::" in name:
            fqn_short_groups[extract_short_name(name).lower()].append(n)
            fqn_scoped_groups[extract_scoped_name(name).lower()].append(n)
    fqn_short_dup = {k: v for k, v in fqn_short_groups.items() if len(v) >= 2}
    fqn_scoped_dup = {k: v for k, v in fqn_scoped_groups.items() if len(v) >= 2}

    # === 7. 크로스-문서 노드 비율 ===
    cross_doc_nodes = sum(1 for dids in node_doc_links.values() if len(dids) >= 2)
    cross_doc_ratio = (
        cross_doc_nodes / len(nodes) if nodes else 0.0
    )

    # === 8. 고아 엣지 (참조하는 노드가 graph_nodes 테이블에 없음) ===
    node_ids = {n["id"] for n in nodes}
    orphan_edges = [
        e for e in edges
        if e["source_node_id"] not in node_ids or e["target_node_id"] not in node_ids
    ]

    # === 메트릭 산출 ===
    duplication_ratio_surface = (
        surface_dup_nodes / len(nodes) if nodes else 0.0
    )
    duplication_ratio_surface_same_type = (
        sum(len(v) for v in surface_same_type_dup.values()) / len(nodes)
        if nodes else 0.0
    )

    # === 결과 ===
    result = {
        "schema_status": {
            "has_link_table": has_link_table,
            "note": (
                "graph_node_documents 테이블이 존재 — 신스키마"
                if has_link_table
                else "graph_node_documents 테이블 부재 — 레거시 스키마 (앱이 한 번도 신스키마로 초기화되지 않음). "
                "node_doc_links 는 graph_nodes.document_id 를 사용함."
            ),
        },
        "stats": {
            "total_documents": doc_count,
            "documents_with_graph_nodes": len(docs_with_nodes),
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "avg_nodes_per_doc_with_graph": round(avg_nodes_per_doc, 2),
            "entity_type_distribution": dict(sorted(
                type_counter.items(), key=lambda x: -x[1],
            )),
            "relation_type_distribution": {
                rt: sum(1 for e in edges if e["relation_type"] == rt)
                for rt in sorted(
                    {e["relation_type"] for e in edges},
                    key=lambda x: -sum(1 for e in edges if e["relation_type"] == x),
                )
            },
            "leading_or_trailing_whitespace_nodes": [
                {"id": n["id"], "name": n["entity_name"],
                 "type": n["entity_type"]}
                for n in nodes
                if (n["entity_name"] or "") != (n["entity_name"] or "").strip()
            ],
        },
        "duplication_surface": {
            "group_count": len(surface_dup_groups),
            "affected_nodes": surface_dup_nodes,
            "ratio": round(duplication_ratio_surface, 4),
            "groups": [
                {
                    "normalized_key": k,
                    "members": [
                        {"id": x["id"], "name": x["entity_name"],
                         "type": x["entity_type"]}
                        for x in v
                    ],
                }
                for k, v in sorted(
                    surface_dup_groups.items(),
                    key=lambda kv: -len(kv[1]),
                )[:20]
            ],
        },
        "duplication_surface_same_type": {
            "group_count": len(surface_same_type_dup),
            "affected_nodes": sum(len(v) for v in surface_same_type_dup.values()),
            "ratio": round(duplication_ratio_surface_same_type, 4),
            "groups": [
                {
                    "normalized_key": k[0],
                    "type": k[1],
                    "members": [
                        {"id": x["id"], "name": x["entity_name"]}
                        for x in v
                    ],
                }
                for k, v in surface_same_type_dup.items()
            ],
        },
        "type_conflicts": {
            "exact_lower_match_with_diff_type": {
                "group_count": len(type_conflict_groups),
                "groups": [
                    {
                        "key": k,
                        "members": [
                            {"id": x["id"], "name": x["entity_name"],
                             "type": x["entity_type"]}
                            for x in v
                        ],
                    }
                    for k, v in type_conflict_groups.items()
                ],
            },
            "surface_match_with_diff_type": {
                "group_count": len(type_conflict_after_surface),
                "groups": [
                    {
                        "key": k,
                        "members": [
                            {"id": x["id"], "name": x["entity_name"],
                             "type": x["entity_type"]}
                            for x in v
                        ],
                    }
                    for k, v in type_conflict_after_surface.items()
                ],
            },
        },
        "fuzzy_candidates": {
            "count": len(fuzzy_pairs),
            "threshold": 0.85,
            "method": "SequenceMatcher ratio after surface normalization",
            "pairs": fuzzy_pairs[:30],
        },
        "fqn_handling": {
            "fqn_nodes_total": sum(
                1 for n in nodes if "::" in (n["entity_name"] or "")
            ),
            "short_name_collisions": {
                "group_count": len(fqn_short_dup),
                "groups": [
                    {
                        "short_name": k,
                        "members": [
                            {"id": x["id"], "name": x["entity_name"],
                             "type": x["entity_type"]}
                            for x in v
                        ],
                    }
                    for k, v in fqn_short_dup.items()
                ][:20],
            },
            "scoped_name_collisions": {
                "group_count": len(fqn_scoped_dup),
            },
        },
        "merge_realization": {
            "cross_document_nodes": cross_doc_nodes,
            "cross_document_ratio": round(cross_doc_ratio, 4),
            "note": (
                "신스키마(graph_node_documents) 부재 시 모든 노드는 단일 문서에 종속 —"
                " 크로스-문서 병합 실현 여부를 측정 불가."
                if not has_link_table else
                "각 노드가 평균 몇 개 문서와 연결되는지 = 병합이 실제로 일어났는지의 지표."
            ),
        },
        "integrity": {
            "orphan_edge_count": len(orphan_edges),
            "orphan_edges": orphan_edges[:10],
        },
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
