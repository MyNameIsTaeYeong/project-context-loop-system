"""R3 — 동치 그룹 채점 (eval_search._reduce_equivalence) 단위 테스트.

설계 §3 — metrics.py 는 무변경이고, eval_search 전처리가 동치 그룹을 '대표
1개' 로 축약한 뒤 기존 메트릭을 그대로 호출한다. 여기서는 축약 후 메트릭이
동치 의미(그룹=정답 1단위, OR=하나면 hit, AND=모두 필요)를 만족하는지 검증.
"""

from __future__ import annotations

import sys
from pathlib import Path

# eval_search 는 scripts/ 에 있으므로 sys.path 보정.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
if str(_PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import eval_search  # type: ignore[import-not-found]  # noqa: E402

from context_loop.eval.metrics import (  # noqa: E402
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

_reduce = eval_search._reduce_equivalence


def test_reduce_or_group_recall() -> None:
    """groups=[[3,5]], retrieved=[5] → recall=1.0 (평탄 채점이면 0.5)."""
    retrieved, relevant = _reduce([5], [[3, 5]])
    assert len(relevant) == 1  # 그룹 1개 = 정답 1단위
    assert recall_at_k(retrieved, relevant, 10) == 1.0


def test_reduce_precision_cap() -> None:
    """groups=[[3,5]], retrieved=[3,5] top2 → precision=0.5 (hits 캡=1)."""
    retrieved, relevant = _reduce([3, 5], [[3, 5]])
    # 같은 그룹 두 멤버는 첫 등장만 남김 → top-2 안에서 hit 1개
    assert precision_at_k(retrieved, relevant, 2) == 0.5


def test_reduce_and_groups() -> None:
    """groups=[[3],[9]], retrieved=[3] → recall=0.5 (2 단위 중 1개)."""
    retrieved, relevant = _reduce([3], [[3], [9]])
    assert len(relevant) == 2
    assert recall_at_k(retrieved, relevant, 10) == 0.5
    # 둘 다 찾으면 1.0
    retrieved2, relevant2 = _reduce([3, 9], [[3], [9]])
    assert recall_at_k(retrieved2, relevant2, 10) == 1.0


def test_reduce_mrr_first_member() -> None:
    """groups=[[3,5]], retrieved=[7,5] → 대표=5(첫 등장), mrr=0.5."""
    retrieved, relevant = _reduce([7, 5], [[3, 5]])
    assert mrr(retrieved, relevant) == 0.5


def test_reduce_ndcg_idcg_denom() -> None:
    """groups=[[3,5],[9]] → idcg 가 2 단위 기준 (relevant 크기 2)."""
    retrieved, relevant = _reduce([3, 9], [[3, 5], [9]])
    assert len(relevant) == 2
    # 두 그룹 모두 top-2 안 → ndcg=1.0
    assert ndcg_at_k(retrieved, relevant, 2) == 1.0


def test_reduce_preserves_non_answer_docs() -> None:
    """그룹 외 doc 는 retrieved' 에 보존 → precision 분모 정확."""
    retrieved, relevant = _reduce([3, 100, 5], [[3, 5]])
    # 3 은 그룹 대표로 유지, 100 은 그대로, 5 는 같은 그룹 중복이라 drop
    assert retrieved == [3, 100]
    assert precision_at_k(retrieved, relevant, 2) == 0.5


def test_reduce_miss_group_counts_in_denominator() -> None:
    """retrieved 에 그룹 멤버가 하나도 없으면 miss 로 recall 분모에 잡힘."""
    retrieved, relevant = _reduce([99], [[3, 5], [9]])
    assert len(relevant) == 2
    assert recall_at_k(retrieved, relevant, 10) == 0.0


def test_no_groups_fallback_flat() -> None:
    """groups=[] 폴백은 evaluate_one 분기에서 평탄 채점과 bit-identical.

    _reduce_equivalence 는 groups 가 비었을 때 호출되지 않으므로(§3.3 분기),
    여기서는 빈 그룹 입력 시 relevant 가 빈 set 이 됨을 확인하여 분기 가드의
    필요성을 고정한다.
    """
    retrieved, relevant = _reduce([3, 5], [])
    assert relevant == set()
    # 빈 그룹은 evaluate_one 에서 호출되지 않음 — 평탄 경로가 set(doc_ids) 사용.


def test_reduce_is_deterministic() -> None:
    """같은 입력 → 같은 출력 (순수 함수)."""
    a = _reduce([7, 5, 3], [[3, 5], [9]])
    b = _reduce([7, 5, 3], [[3, 5], [9]])
    assert a == b
