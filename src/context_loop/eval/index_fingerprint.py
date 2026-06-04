"""인덱스/코퍼스 지문(fingerprint) — 절대 점수 앵커링.

평가 산출물(summary.json, frozen benchmark manifest)에 "이 점수가 어느 코퍼스/
인덱스 위에서 나왔는가"를 기계검증 가능한 형태로 못 박기 위한 결정적 해시를
계산한다. 절대 점수를 신뢰하려면 동일 코퍼스에서 측정됐음을 보장해야 하는데,
이 모듈이 그 정체성(identity)을 제공한다.

설계 원칙:

* **결정성** — 같은 인덱스면 항상 같은 sha256. 임베딩 float 처럼 직렬화가
  비결정적인 값은 해시에 넣지 않고, 안정적인 식별 메타(id / document_id / view /
  entity_name 등)만 정렬 후 canonical JSON 으로 해시한다.
* **best-effort** — 스토어 조회 실패 시 예외를 전파하지 않고 ``sha256=""`` 으로
  폴백한다(평가 자체를 막지 않기 위함). 호출부는 빈 문자열을 "지문 없음"으로
  취급한다.
* **대용량 안전** — 벡터 수가 매우 크면 전체 metadata 로드가 메모리를 압박할 수
  있어, 임계 초과 시 결정적 샘플 + 카운트로 폴백한다(샘플 여부를 함께 보고).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 전체 metadata 를 한 번에 로드할 최대 벡터 수. 초과 시 결정적 샘플로 폴백.
MAX_FULL_SCAN_VECTORS = 200_000
# 폴백 샘플 크기(결정적 — id 정렬 후 균등 간격 추출).
FALLBACK_SAMPLE_SIZE = 20_000


def _sha256_canonical(obj: Any) -> str:
    """객체를 canonical JSON 으로 직렬화 후 sha256 hexdigest 를 반환한다."""
    payload = json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def vector_store_fingerprint(vector_store: Any) -> dict[str, Any]:
    """벡터 스토어의 결정적 지문.

    ``collection.get(include=["metadatas"])`` 로 전체 id + 안정 메타
    ``(document_id, view, section_path)`` 만 뽑아 id 정렬 후 sha256. 임베딩
    float 는 제외한다(직렬화 비결정성).

    Returns:
        ``{"n_vectors": int, "sha256": str, "sampled": bool}``. 조회 실패 시
        ``sha256=""``.
    """
    try:
        n_vectors = int(vector_store.count())
    except Exception:
        logger.warning("vector_store.count() 실패 — 지문 생략", exc_info=True)
        return {"n_vectors": 0, "sha256": "", "sampled": False}

    sampled = n_vectors > MAX_FULL_SCAN_VECTORS
    try:
        result = vector_store.collection.get(include=["metadatas"])
    except Exception:
        logger.warning("vector_store.collection.get 실패 — 지문 생략", exc_info=True)
        return {"n_vectors": n_vectors, "sha256": "", "sampled": False}

    ids = result.get("ids", []) or []
    metas = result.get("metadatas", []) or []

    rows: list[tuple[str, Any, Any, Any]] = []
    for i, vec_id in enumerate(ids):
        meta = metas[i] if i < len(metas) and metas[i] else {}
        rows.append(
            (
                str(vec_id),
                meta.get("document_id"),
                meta.get("view"),
                meta.get("section_path"),
            )
        )
    # id 기준 정렬로 도착 순서 비결정성을 제거.
    rows.sort(key=lambda r: r[0])

    if sampled:
        # 결정적 균등 샘플 — id 정렬된 rows 에서 균등 간격 추출.
        step = max(1, len(rows) // FALLBACK_SAMPLE_SIZE)
        rows = rows[::step]

    digest = _sha256_canonical(rows)
    return {"n_vectors": n_vectors, "sha256": digest, "sampled": sampled}


def graph_store_fingerprint(graph_store: Any) -> dict[str, Any]:
    """그래프 스토어의 결정적 지문.

    ``GraphStore.content_fingerprint()`` 에 위임한다(노드/엣지 안정 키 기반).
    실패 시 ``sha256=""``.
    """
    try:
        return graph_store.content_fingerprint()
    except Exception:
        logger.warning("graph_store.content_fingerprint 실패 — 지문 생략", exc_info=True)
        return {"nodes": 0, "edges": 0, "sha256": ""}


async def corpus_fingerprint(meta_store: Any) -> dict[str, Any]:
    """메타데이터 스토어(문서 코퍼스)의 결정적 지문.

    ``list_documents()`` 로 전체 문서를 받아 ``(id, source_type, content_hash 또는
    updated_at)`` 안정 키만 정렬 후 sha256. 실패 시 ``sha256=""``.
    """
    try:
        docs = await meta_store.list_documents()
    except Exception:
        logger.warning("meta_store.list_documents 실패 — 지문 생략", exc_info=True)
        return {"n_documents": 0, "sha256": ""}

    rows: list[tuple[Any, Any, Any]] = []
    for d in docs:
        # content_hash 가 있으면 우선(내용 변경 민감), 없으면 updated_at 폴백.
        version = d.get("content_hash") or d.get("updated_at")
        rows.append((d.get("id"), d.get("source_type"), version))
    rows.sort(key=lambda r: (str(r[0])))

    digest = _sha256_canonical(rows)
    return {"n_documents": len(docs), "sha256": digest}


async def combined_index_fingerprint(
    vector_store: Any,
    graph_store: Any,
    meta_store: Any,
) -> dict[str, Any]:
    """벡터 + 그래프 + 코퍼스 지문을 하나로 묶는다.

    Returns:
        ``{"vector": {...}, "graph": {...}, "corpus": {...}}`` 중첩 dict.
    """
    return {
        "vector": vector_store_fingerprint(vector_store),
        "graph": graph_store_fingerprint(graph_store),
        "corpus": await corpus_fingerprint(meta_store),
    }
