"""Phase 0 — compare_runs 의 인덱스 앵커 동치성 검증 단위 테스트.

A/B 비교 시 같은 코퍼스/인덱스에서 측정됐는지(vector/corpus/graph 지문 일치)를
EQUIVALENCE_KEYS 가 강제하는지 확인한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
if str(_PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import compare_runs  # type: ignore[import-not-found]  # noqa: E402


def _cfg() -> dict:
    return {
        "gold_set_sha256": "g",
        "embedding_model": "e",
        "llm_model": "l",
        "top_k": 5,
        "max_chunks": 10,
        "similarity_threshold": 0.0,
        "rerank_enabled": False,
        "hyde_enabled": False,
        "judge_mode": "reference-free",
        "vector_store_sha256": "V1",
        "corpus_sha256": "C1",
        "graph_store_sha256": "G1",
    }


def test_index_anchor_keys_present() -> None:
    for k in ("vector_store_sha256", "corpus_sha256", "graph_store_sha256"):
        assert k in compare_runs.EQUIVALENCE_KEYS


def test_equal_configs_no_diff() -> None:
    assert compare_runs.check_equivalence(_cfg(), _cfg()) == []


def test_vector_drift_detected() -> None:
    a = _cfg()
    b = _cfg()
    b["vector_store_sha256"] = "V2"
    diffs = compare_runs.check_equivalence(a, b)
    assert any("vector_store_sha256" in d for d in diffs)


def test_corpus_drift_detected() -> None:
    a = _cfg()
    b = _cfg()
    b["corpus_sha256"] = "C2"
    diffs = compare_runs.check_equivalence(a, b)
    assert any("corpus_sha256" in d for d in diffs)
