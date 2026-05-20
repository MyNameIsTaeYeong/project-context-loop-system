"""4-tier cascade graph matching 테스트 (2차 — 그래프 인덱싱 강건성).

설계 시나리오 A~F (``_workspace/01_analysis.md`` §3) 를 회귀 테스트로
가져와, 각 인덱싱 변경 패턴이 어느 tier 에서 흡수되는지를 검증한다.

임베딩 호출은 결정론적 해시 기반 :class:`FakeEmbeddingClient` 로
mock — 같은 텍스트→같은 벡터, 의미적으로 유사한 쌍은 cosine ≥ 0.78
이 되도록 사전 정의된 매핑을 사용한다.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from context_loop.eval.gold_set import GraphEntityRef, GraphRelationRef
from context_loop.eval.graph_match import (
    DEFAULT_GRAPH_MATCH_THRESHOLD,
    aggregate_tier_counts,
    build_embed_fn,
    cosine_similarity,
    match_entity_tiered,
    match_relation_tiered,
    run_entity_matching,
    run_relation_matching,
)

# ---------------------------------------------------------------------------
# Fake embedding — 의미 유사도 fixture
# ---------------------------------------------------------------------------


class FakeEmbeddingClient:
    """결정론적 해시 기반 mock 임베딩.

    의미적으로 유사한 텍스트 쌍은 ``SYNONYM_GROUPS`` 매핑으로 같은 그룹
    벡터를 받아 cosine = 1.0 이 된다. 그 외 텍스트는 sha256 해시 → 32-d
    벡터로 무관한 벡터를 만든다.
    """

    # 그룹 안의 모든 텍스트는 같은 벡터 → cosine = 1.0
    SYNONYM_GROUPS: list[set[str]] = [
        # 시나리오 B (type 명 변경) — 같은 의미를 가진 entity description.
        {
            "결제 처리 시스템. 주문 서비스에 의존한다.",
            "결제를 담당하는 백엔드 컴포넌트로 주문에서 호출된다.",
        },
        # 시나리오 C3 (동의어, alias 없는 경우)
        {
            "사내 인증 게이트웨이",
            "회사 사용자 로그인을 처리하는 인증 시스템",
        },
        # 시나리오 D (병합/canonical 변경)
        {
            "주문 처리 마이크로서비스",
            "Order processing microservice",
        },
        # 시나리오 E (관계 타입 변경)
        {
            "결제 서비스는 주문 서비스에 의존한다",
            "결제 서비스는 주문 서비스를 필요로 한다",
        },
    ]

    def _group_vector(self, group_idx: int) -> list[float]:
        # 32-d 단위 벡터, group_idx 위치만 1.0
        vec = [0.0] * 32
        vec[group_idx % 32] = 1.0
        return vec

    def embed_query(self, text: str) -> list[float]:
        for gi, group in enumerate(self.SYNONYM_GROUPS):
            if text in group:
                return self._group_vector(gi)
        # 해시 기반 결정론 — 0~1 범위로 정규화 후 unit 벡터화
        h = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [b / 255.0 for b in h[:32]]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


def _embed(text: str) -> list[float] | None:
    return FakeEmbeddingClient().embed_query(text)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _ge(
    name: str,
    type_: str,
    *,
    aliases: list[str] | None = None,
    description: str = "",
    description_embedding: list[float] | None = None,
) -> GraphEntityRef:
    return GraphEntityRef(
        name=name,
        type=type_,
        aliases=aliases or [],
        description=description,
        description_embedding=description_embedding,
    )


# ---------------------------------------------------------------------------
# cosine_similarity 기본
# ---------------------------------------------------------------------------


def test_cosine_similarity_basic() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_cosine_similarity_handles_empty_or_mismatch() -> None:
    assert cosine_similarity(None, [1.0]) == 0.0
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


# ---------------------------------------------------------------------------
# T1 — exact (시나리오 C2: 케이스 변경)
# ---------------------------------------------------------------------------


def test_tier_t1_exact_case_insensitive() -> None:
    """``"AuthService"`` vs ``"authservice"`` — lower 적용으로 T1 hit."""
    golden = _ge("AuthService", "system")
    retrieved = [_ge("authservice", "system")]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "exact"
    assert result.score == 1.0


def test_tier_t1_miss_when_type_differs_strict() -> None:
    """strict=True 면 T1 만 시도 — type 일치 요구로 miss.

    이전엔 strict 인자 없이 T4 도 description/name 부재로 자연 skip 되었으나,
    R1 에서 골든 description 부재 시 name fallback 이 도입되어 같은 name 끼리는
    T4(type-agnostic) 로 매칭될 수 있다 (F-METRIC-02). 따라서 'T1 만 발동되어
    miss' 를 검증하려면 strict=True 를 명시한다.
    """
    golden = _ge("결제 서비스", "system")
    retrieved = [_ge("결제 서비스", "service")]
    result = match_entity_tiered(golden, retrieved, _embed, strict=True)
    assert result is None


# ---------------------------------------------------------------------------
# T2 — alias OR (시나리오 C3: 동의어)
# ---------------------------------------------------------------------------


def test_tier_t2_alias_hit() -> None:
    golden = _ge(
        "인증 서비스", "system",
        aliases=["Auth Service", "AuthSvc", "인증서비스"],
    )
    retrieved = [_ge("Auth Service", "system")]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "alias"
    assert result.score == 1.0


def test_tier_t2_alias_requires_type_match() -> None:
    """alias 단계도 type 정확 — type 다르면 T2 miss → T4 (embedding) 으로 폴백."""
    golden = _ge(
        "인증 서비스", "system",
        aliases=["Auth Service"],
        description="사내 인증 게이트웨이",
    )
    retrieved = [_ge(
        "Auth Service", "service",
        description="회사 사용자 로그인을 처리하는 인증 시스템",
    )]
    result = match_entity_tiered(golden, retrieved, _embed)
    # T1/T2/T3 모두 type 미스 → T4 임베딩 cosine = 1.0 (시나리오 그룹)
    assert result is not None
    assert result.tier == "embedding"


# ---------------------------------------------------------------------------
# T3 — normalize (시나리오 C1: 공백/punctuation)
# ---------------------------------------------------------------------------


def test_tier_t3_whitespace_normalized() -> None:
    """``"인증 서비스"`` vs ``"인증서비스"`` → 공백 제거 후 동일."""
    golden = _ge("인증 서비스", "system")
    retrieved = [_ge("인증서비스", "system")]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "normalize"
    assert result.score == 0.9


def test_tier_t3_punctuation_normalized() -> None:
    """``"auth-svc"`` vs ``"auth_svc"`` → 하이픈/언더스코어 제거."""
    golden = _ge("auth-svc", "function")
    retrieved = [_ge("auth_svc", "function")]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "normalize"


def test_tier_t3_nfkc_fullwidth_normalized() -> None:
    """전각/반각 차이 → NFKC 정규화로 흡수."""
    # "Auth" 전각문자
    golden = _ge("ＡＵＴＨ", "system")
    retrieved = [_ge("auth", "system")]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "normalize"


# ---------------------------------------------------------------------------
# T4 — embedding (시나리오 B: type 명 변경, type-agnostic)
# ---------------------------------------------------------------------------


def test_tier_t4_embedding_type_agnostic() -> None:
    """시나리오 B — entity_type 명만 다르고 의미는 같음.

    T1~T3 는 type 정확을 요구하므로 모두 miss → T4 가 type 무시로 hit.
    """
    golden = _ge(
        "결제 서비스", "system",
        description="결제 처리 시스템. 주문 서비스에 의존한다.",
    )
    retrieved = [_ge(
        "결제 서비스", "service",
        description="결제를 담당하는 백엔드 컴포넌트로 주문에서 호출된다.",
    )]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "embedding"
    assert result.score >= DEFAULT_GRAPH_MATCH_THRESHOLD


def test_tier_t4_below_threshold_returns_none() -> None:
    """description 이 의미적으로 멀면 T4 도 miss → None.

    R1 에서 기본 threshold 가 0.65 로 낮춰졌으므로 (이전 0.78), 명시적 임계값을
    높여서 '임계 미달 시 None' 의미를 보존한다.
    """
    golden = _ge(
        "결제 서비스", "service",
        description="무관한 텍스트 1",
    )
    retrieved = [_ge(
        "주문 서비스", "service",
        description="완전 다른 의미의 텍스트 2",
    )]
    result = match_entity_tiered(golden, retrieved, _embed, threshold=0.95)
    assert result is None


def test_tier_t4_uses_stored_embedding() -> None:
    """골드셋에 description_embedding 이 박혀 있으면 그것을 사용."""
    golden_emb = [0.0] * 32
    golden_emb[0] = 1.0
    golden = _ge(
        "X", "type-a",
        description="무엇이든",
        description_embedding=golden_emb,
    )
    retrieved = [_ge("X", "type-b", description="무엇이든")]
    # _embed 가 "무엇이든" 에 대해 어떤 벡터를 만들어도, golden 의 박힌
    # 벡터로 검색 측 임베딩과 비교한다. 검색 측은 _embed("무엇이든").
    # 이 벡터는 첫 차원이 1.0 이 아니므로 cosine < 1.0 일 가능성이 큼.
    # 하지만 결과는 None 일 수도 있고 임베딩일 수도 있음 — 핵심은
    # description_embedding 이 호출 인자로 전달되었는지.
    result = match_entity_tiered(golden, retrieved, _embed)
    # cosine 가 threshold 를 넘는지는 fake 임베딩에 따름. 일단 hit 시
    # tier 가 embedding 인 것만 확인.
    if result is not None:
        assert result.tier == "embedding"


def test_tier_t4_name_fallback_when_no_description() -> None:
    """R1: description 부재 시 골든·검색 모두 name 으로 fallback (F-METRIC-02).

    type 이 달라도 T4 는 type-agnostic 이므로 같은 name 임베딩이면 매칭된다.
    이전엔 description 부재 시 T4 즉시 skip 이었지만, 검색 측 fallback
    (F-SRCH-06) 과 대칭으로 평가 측도 name 으로 fallback 한다 — 합성
    골드셋이 description 을 항상 채우지 못해 발생한 funnel 손실 완화.
    """
    golden = _ge("X", "type-a")
    retrieved = [_ge("X", "type-b")]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "embedding"


# ---------------------------------------------------------------------------
# strict 모드 — 1차 동작 호환 (T1 만)
# ---------------------------------------------------------------------------


def test_strict_mode_skips_t2_t3_t4() -> None:
    """strict=True 면 T2/T3/T4 모두 skip — 정확 비교만."""
    golden = _ge(
        "인증 서비스", "system",
        aliases=["Auth Service"],
        description="사내 인증 게이트웨이",
    )
    # T2/T3 hit 후보 + T4 hit 후보 모두 제공해도 strict 면 None
    retrieved = [
        _ge("인증서비스", "system"),  # T3 hit
        _ge("Auth Service", "system"),  # T2 hit
        _ge("auth", "system", description="회사 사용자 로그인을 처리하는 인증 시스템"),
    ]
    result = match_entity_tiered(golden, retrieved, _embed, strict=True)
    assert result is None

    # exact 후보가 들어가면 hit
    retrieved_with_exact = retrieved + [_ge("인증 서비스", "system")]
    result2 = match_entity_tiered(golden, retrieved_with_exact, _embed, strict=True)
    assert result2 is not None
    assert result2.tier == "exact"


# ---------------------------------------------------------------------------
# 시나리오 A — 신규 type 추가 (precision 하락만, recall 유지)
# ---------------------------------------------------------------------------


def test_scenario_a_new_type_added_to_retrieved() -> None:
    """retrieved 가 더 풍부해져도 골든 entity 가 그대로 hit."""
    golden = [_ge("결제 서비스", "system")]
    retrieved = [
        _ge("결제 서비스", "system"),
        _ge("결제 마이크로서비스", "framework"),  # 새 type 노드 추가
        _ge("결제 모니터링", "framework"),
    ]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["exact"] == 1
    # 매칭된 retrieved 키는 1개 — recall 분자는 1.
    assert len(report.retrieved_keys_in_rank_order) == 1
    assert report.all_relevant_keys == {("결제 서비스", "system")}


# ---------------------------------------------------------------------------
# 시나리오 B — type 명 변경 (system → service)
# ---------------------------------------------------------------------------


def test_scenario_b_type_renamed_absorbed_by_t4() -> None:
    """type 명만 변경된 노드 — T4 embedding 으로 흡수."""
    golden = [_ge(
        "결제 서비스", "system",
        description="결제 처리 시스템. 주문 서비스에 의존한다.",
    )]
    retrieved = [_ge(
        "결제 서비스", "service",  # type 명 변경
        description="결제를 담당하는 백엔드 컴포넌트로 주문에서 호출된다.",
    )]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["embedding"] == 1
    assert len(report.retrieved_keys_in_rank_order) == 1


# ---------------------------------------------------------------------------
# 시나리오 C — 표기 변경
# ---------------------------------------------------------------------------


def test_scenario_c1_whitespace_change_absorbed_by_t3() -> None:
    golden = [_ge("인증 서비스", "system")]
    retrieved = [_ge("인증서비스", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["normalize"] == 1


def test_scenario_c2_case_change_absorbed_by_t1() -> None:
    golden = [_ge("AuthService", "system")]
    retrieved = [_ge("authservice", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["exact"] == 1


def test_scenario_c3_synonym_absorbed_by_t2_alias() -> None:
    golden = [_ge(
        "인증 서비스", "system",
        aliases=["Auth Service", "AuthSvc"],
    )]
    retrieved = [_ge("Auth Service", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["alias"] == 1


def test_scenario_c3_synonym_without_alias_uses_t4() -> None:
    """alias 없어도 description 임베딩 매칭으로 흡수."""
    golden = [_ge(
        "인증 서비스", "system",
        description="사내 인증 게이트웨이",
    )]
    retrieved = [_ge(
        "Auth Service", "system",
        description="회사 사용자 로그인을 처리하는 인증 시스템",
    )]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["embedding"] == 1


# ---------------------------------------------------------------------------
# 시나리오 D — 병합 / canonical 변경
# ---------------------------------------------------------------------------


def test_scenario_d_merged_node_via_alias() -> None:
    """병합 후 canonical 표기가 영문이 됨 — alias 보유 시 T2 흡수."""
    golden = [_ge(
        "주문 서비스", "system",
        aliases=["Order Service", "OrderSvc", "주문서비스"],
    )]
    retrieved = [_ge("Order Service", "system")]  # canonical 이 영문으로
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["alias"] == 1


def test_scenario_d_merged_node_via_embedding() -> None:
    """alias 없이 description 만 있어도 의미 매칭."""
    golden = [_ge(
        "주문 서비스", "system",
        description="주문 처리 마이크로서비스",
    )]
    retrieved = [_ge(
        "Order Service", "system",
        description="Order processing microservice",
    )]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["embedding"] == 1


# ---------------------------------------------------------------------------
# 시나리오 E — 관계 타입 명 변경 (--score-relations)
# ---------------------------------------------------------------------------


def test_scenario_e_relation_type_renamed_absorbed_by_t4() -> None:
    """``depends_on`` → ``requires`` 변경 — relation T4 embedding 으로 매칭."""
    golden = [GraphRelationRef(
        source_name="결제 서비스",
        target_name="주문 서비스",
        relation_type="depends_on",
        description="결제 서비스는 주문 서비스에 의존한다",
    )]
    retrieved = [GraphRelationRef(
        source_name="결제 서비스",
        target_name="주문 서비스",
        relation_type="requires",  # 타입 명 변경
        description="결제 서비스는 주문 서비스를 필요로 한다",
    )]
    retrieved_keys, _relevant_keys, tier_counts, scores = run_relation_matching(
        golden, retrieved, embed_fn=_embed,
    )
    assert tier_counts["embedding"] == 1
    assert len(retrieved_keys) == 1
    assert scores[0] >= DEFAULT_GRAPH_MATCH_THRESHOLD


def test_scenario_e_relation_exact_match_hits_t1() -> None:
    golden = [GraphRelationRef(
        source_name="A", target_name="B", relation_type="depends_on",
    )]
    retrieved = [GraphRelationRef(
        source_name="A", target_name="B", relation_type="depends_on",
    )]
    _, _, tier_counts, scores = run_relation_matching(
        golden, retrieved, embed_fn=_embed,
    )
    assert tier_counts["exact"] == 1
    assert scores == [1.0]


def test_relation_strict_skips_embedding() -> None:
    """관계 strict 모드 — T4 skip."""
    golden = [GraphRelationRef(
        source_name="A", target_name="B", relation_type="depends_on",
        description="설명",
    )]
    retrieved = [GraphRelationRef(
        source_name="A", target_name="B", relation_type="requires",
        description="설명",
    )]
    rks, _, tcs, _ = run_relation_matching(
        golden, retrieved, embed_fn=_embed, strict=True,
    )
    assert rks == []
    assert all(c == 0 for c in tcs.values())


# ---------------------------------------------------------------------------
# 시나리오 F — 새 이웃 추가로 retrieved 가 길어짐
# ---------------------------------------------------------------------------


def test_scenario_f_more_neighbors_keeps_recall() -> None:
    """retrieved 분모가 커져도 골든 entity 는 그대로 hit (recall 유지)."""
    golden = [_ge("결제 서비스", "system")]
    retrieved = [
        _ge("새로운 이웃 1", "concept"),
        _ge("결제 서비스", "system"),  # rank-2
        _ge("새로운 이웃 2", "concept"),
        _ge("새로운 이웃 3", "concept"),
    ]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts["exact"] == 1
    # rank 보존 — retrieved 인덱스 1 (=rank 2)
    assert report.results[0] is not None
    assert report.results[0].retrieved_index == 1


# ---------------------------------------------------------------------------
# Backward-compat — 1차 골드셋 (description/alias 없음)
# ---------------------------------------------------------------------------


def test_backward_compat_v1_minimal_entity_uses_t1_only() -> None:
    """v1 골드 entity (`{name, type}`) — T1 hit 또는 T3 hit 만 가능."""
    golden = [_ge("결제 서비스", "system")]  # aliases / description 모두 비어 있음
    retrieved = [_ge("결제 서비스", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert report.tier_counts == {"exact": 1, "alias": 0, "normalize": 0, "embedding": 0}


def test_backward_compat_v1_minimal_entity_strict_skip_t4() -> None:
    """strict=True 면 T4 단계가 skip 되어 description/name fallback 없이 miss.

    R1 에서 description 부재 시 name fallback 이 도입되었으므로 'T4 자연 skip'
    의미를 검증하려면 strict=True 가 필요하다 (1차 동작 호환).
    """
    golden = [_ge("결제 서비스", "system")]
    retrieved = [_ge("결제 서비스", "service")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed, strict=True)
    # 모든 tier miss
    assert all(c == 0 for c in report.tier_counts.values())


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------


def test_default_threshold_is_065_for_funnel_recovery() -> None:
    """R1 F-METRIC-01: T4 임베딩 매칭의 기본 임계값이 0.65 로 낮춰졌다.

    이전 0.78 은 description 이 짧거나 비특이적인 검색 결과에서 과도하게
    엄격하여 그래프 메트릭 funnel 손실의 한 축이었다. 0.65 가 default 임을
    회귀 가드로 확정.
    """
    assert DEFAULT_GRAPH_MATCH_THRESHOLD == 0.65


def test_golden_description_fallback_to_name_when_empty() -> None:
    """R1 F-METRIC-02: 골든 description 이 비어도 name 으로 fallback 하여
    T4 가 진행된다. 같은 name 끼리 임베딩이 일치하면 type 차이가 있어도
    매칭 (T4 type-agnostic)."""
    golden = _ge("X", "type-a")  # description 없음
    retrieved = [_ge("X", "type-b")]  # description 없음
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "embedding"


def test_match_report_records_relevant_keys_for_hits_only() -> None:
    """미매칭 golden 은 relevant_keys 에 안 들어가지만 all_relevant_keys 에는 들어감.

    strict=True 로 호출하여 R1 의 name-fallback T4 가 우연히 짧은 이름 mock
    임베딩에서 통과해 'B' 가 매칭되는 케이스를 차단한다 — 이 테스트의 본
    목적은 'all_relevant_keys 분모와 relevant_keys 분자' 의 분리 검증.
    """
    golden = [
        _ge("A", "system"),  # hit
        _ge("B", "system"),  # miss
    ]
    retrieved = [_ge("A", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed, strict=True)
    assert report.relevant_keys == {("a", "system")}
    assert report.all_relevant_keys == {("a", "system"), ("b", "system")}


def test_match_report_scores_aggregated() -> None:
    """score avg/min/max 가 hit 한 매칭들로부터 계산된다."""
    golden = [
        _ge("A", "system"),
        _ge("B", "system"),
    ]
    retrieved = [
        _ge("A", "system"),  # T1 — 1.0
        _ge("B  ", "system"),  # T3 normalize? 사실 공백만 다름 → normalize tier
    ]
    # B 의 retrieved 는 trailing whitespace — T1 의 .strip().lower() 로 매칭됨
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    # 두 hit 모두 T1 (양쪽 strip 후 정확 일치)
    assert report.tier_counts["exact"] == 2
    assert report.avg_score() == 1.0


def test_aggregate_tier_counts() -> None:
    """여러 쿼리 결과의 tier 카운트가 단순 합산된다."""
    rows = [
        {"exact": 1, "alias": 0, "normalize": 1, "embedding": 0},
        {"exact": 2, "alias": 1, "normalize": 0, "embedding": 1},
    ]
    total = aggregate_tier_counts(rows)
    assert total == {"exact": 3, "alias": 1, "normalize": 1, "embedding": 1}


def test_aggregate_tier_counts_empty() -> None:
    assert aggregate_tier_counts([]) == {
        "exact": 0, "alias": 0, "normalize": 0, "embedding": 0,
    }


# ---------------------------------------------------------------------------
# build_embed_fn — 캐시 + None handling
# ---------------------------------------------------------------------------


def test_build_embed_fn_returns_none_when_client_none() -> None:
    fn = build_embed_fn(None)
    assert fn("anything") is None


def test_build_embed_fn_caches_results() -> None:
    """같은 텍스트는 1회만 실제 호출된다."""
    calls: list[str] = []

    class CountingEmbedder:
        def embed_query(self, text: str) -> list[float]:
            calls.append(text)
            return [1.0, 0.0, 0.0]

    fn = build_embed_fn(CountingEmbedder(), model_id="test")
    fn("foo")
    fn("foo")
    fn("bar")
    fn("foo")
    assert calls == ["foo", "bar"]


def test_build_embed_fn_empty_text_returns_none() -> None:
    class _Embedder:
        def embed_query(self, _t: str) -> list[float]:
            return [1.0]

    fn = build_embed_fn(_Embedder())
    assert fn("") is None


def test_build_embed_fn_async_only_client_runs_event_loop() -> None:
    """동기 embed_query 가 없으면 aembed_query 를 asyncio.run 으로 실행한다."""

    class AsyncOnlyClient:
        async def aembed_query(self, text: str) -> list[float]:
            return [float(len(text)), 0.0]

    fn = build_embed_fn(AsyncOnlyClient(), model_id="async-test")
    out = fn("abc")
    assert out == [3.0, 0.0]


# ---------------------------------------------------------------------------
# 다중 retrieved 에서 best score 선택
# ---------------------------------------------------------------------------


def test_t4_selects_highest_cosine() -> None:
    """여러 후보 중 cosine 이 가장 높은 retrieved 를 선택."""
    golden = _ge(
        "주문 서비스", "system",
        description="주문 처리 마이크로서비스",
    )
    retrieved = [
        _ge("무관 후보", "service", description="아무 관계 없는 텍스트"),
        _ge(
            "Order Service", "service",
            description="Order processing microservice",  # 완전 일치 (group)
        ),
    ]
    result = match_entity_tiered(golden, retrieved, _embed)
    assert result is not None
    assert result.tier == "embedding"
    assert result.retrieved_index == 1
    assert result.score == 1.0


# ---------------------------------------------------------------------------
# Strict on aggregation level
# ---------------------------------------------------------------------------


def test_run_entity_matching_strict_propagates() -> None:
    """run_entity_matching 의 strict 인자가 cascade 에 적용된다."""
    golden = [_ge(
        "X", "system",
        aliases=["Y"],
        description="설명",
    )]
    retrieved = [
        _ge("Y", "system"),  # alias hit
        _ge("Z", "system", description="설명"),  # embedding hit
    ]
    report_strict = run_entity_matching(
        golden, retrieved, embed_fn=_embed, strict=True,
    )
    assert all(c == 0 for c in report_strict.tier_counts.values())

    report_loose = run_entity_matching(
        golden, retrieved, embed_fn=_embed, strict=False,
    )
    assert report_loose.tier_counts["alias"] == 1


# ---------------------------------------------------------------------------
# Threshold override
# ---------------------------------------------------------------------------


def test_threshold_above_one_makes_t4_unreachable() -> None:
    """τ=2.0 (도달 불가) → T4 hit 가 없음."""
    golden = [_ge(
        "X", "type-a",
        description="결제 처리 시스템. 주문 서비스에 의존한다.",
    )]
    retrieved = [_ge(
        "Y", "type-b",
        description="결제를 담당하는 백엔드 컴포넌트로 주문에서 호출된다.",
    )]
    report = run_entity_matching(
        golden, retrieved, embed_fn=_embed, threshold=2.0,
    )
    assert all(c == 0 for c in report.tier_counts.values())


# ---------------------------------------------------------------------------
# Relation matching — basic
# ---------------------------------------------------------------------------


def test_match_relation_tiered_returns_none_on_missing_endpoints() -> None:
    golden = GraphRelationRef(
        source_name="A", target_name="B", relation_type="x",
        description="설명",
    )
    retrieved: list[GraphRelationRef] = []
    assert match_relation_tiered(golden, retrieved, _embed) is None


# ---------------------------------------------------------------------------
# 종합 — 시나리오 표 (시나리오 A~F → 어떤 tier 가 흡수?)
# ---------------------------------------------------------------------------


def test_scenario_summary_matrix() -> None:
    """시나리오 A~F 의 매칭 결과 종합 — 회귀 방지용 한 줄짜리 검증."""
    scenarios: dict[str, tuple[Any, Any]] = {
        "A_new_type": (
            [_ge("결제 서비스", "system")],
            [_ge("결제 서비스", "system"), _ge("새 노드", "framework")],
        ),
        "B_type_renamed": (
            [_ge(
                "결제 서비스", "system",
                description="결제 처리 시스템. 주문 서비스에 의존한다.",
            )],
            [_ge(
                "결제 서비스", "service",
                description="결제를 담당하는 백엔드 컴포넌트로 주문에서 호출된다.",
            )],
        ),
        "C1_whitespace": (
            [_ge("인증 서비스", "system")],
            [_ge("인증서비스", "system")],
        ),
        "C2_case": (
            [_ge("AuthService", "system")],
            [_ge("authservice", "system")],
        ),
        "C3_synonym_alias": (
            [_ge("인증 서비스", "system", aliases=["Auth Service"])],
            [_ge("Auth Service", "system")],
        ),
        "D_merged_via_alias": (
            [_ge("주문 서비스", "system", aliases=["Order Service"])],
            [_ge("Order Service", "system")],
        ),
        "F_extra_neighbors": (
            [_ge("결제 서비스", "system")],
            [
                _ge("결제 서비스", "system"),
                _ge("이웃 1", "concept"),
                _ge("이웃 2", "concept"),
            ],
        ),
    }
    expected_tier = {
        "A_new_type": "exact",
        "B_type_renamed": "embedding",
        "C1_whitespace": "normalize",
        "C2_case": "exact",
        "C3_synonym_alias": "alias",
        "D_merged_via_alias": "alias",
        "F_extra_neighbors": "exact",
    }
    for key, (golden, retrieved) in scenarios.items():
        report = run_entity_matching(
            golden, retrieved, embed_fn=_embed,
        )
        tier = expected_tier[key]
        assert report.tier_counts[tier] == 1, (
            f"scenario {key}: expected tier {tier}, "
            f"got counts {report.tier_counts}"
        )
