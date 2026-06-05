"""그래프 엔티티·관계 강건 매칭 (2차 — 그래프 인덱싱 강건성).

4단계 cascade tiered matching:

* **T1 exact**: ``(name.lower().strip(), type.strip())`` 페어 비교.
* **T2 alias**: 골드셋 ``aliases`` 중 하나가 검색 엔티티 이름과 일치 (type 정확).
* **T3 normalize**: NFKC 정규화 + 공백·구두점 제거 후 비교 (type 정확).
* **T4 embedding**: ``description`` 임베딩 cosine ≥ τ. **type 일치 요구하지 않음**
  — 이것이 ``system → service`` 처럼 type 명만 바뀐 시나리오를 흡수.

각 단계에서 hit 하면 즉시 단락하고 hit 한 tier 와 score 를 기록한다. T4 의
임베딩 계산은 한 평가 실행 내에서 ``(model_id, text)`` 키 LRU 캐시로 비용
통제한다.

설계 문서: ``_workspace/02_design.md`` §2 — Tiered Matching 알고리즘.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from context_loop.eval.gold_set import GraphEntityRef, GraphRelationRef

logger = logging.getLogger(__name__)

DEFAULT_GRAPH_MATCH_THRESHOLD = 0.65
"""T4 임베딩 cosine 임계값 기본.

이전 0.78 은 description 이 짧거나(또는 비어서 name 으로 fallback) 임베딩이
비특이적인 retrieved 와의 매칭에서 너무 보수적이었다 — funnel 손실의 한 축.
0.65 는 의미 매칭의 실용 임계값이며, T1~T3 표면 매칭은 영향 없이 T4 단계만
넓힌다. 골드셋 신뢰성에 영향이 큰 변경이므로 별도 validation 권장.
"""

MATCH_TIERS = ("exact", "alias", "normalize", "embedding")
"""기록·집계에 쓰이는 tier 이름. 순서는 cascade 적용 순서."""


# ---------------------------------------------------------------------------
# 정규화 + 임베딩 유틸
# ---------------------------------------------------------------------------


_NORMALIZE_STRIP_RE = re.compile(r"[\s\-\_\.]+")


@lru_cache(maxsize=4096)
def _normalize(text: str) -> str:
    """T3 단계의 정규화 — NFKC + lower + 공백·하이픈·언더스코어·점 제거."""
    if not text:
        return ""
    nfkc = unicodedata.normalize("NFKC", text)
    return _NORMALIZE_STRIP_RE.sub("", nfkc).lower()


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    """두 벡터의 코사인 유사도. 길이가 다르거나 None 이면 0.0."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """단일 골든 엔티티의 매칭 결과.

    Attributes:
        retrieved_index: hit 한 retrieved 엔티티의 list 인덱스 (rank-1).
        tier: hit 한 tier 이름 (``"exact"`` / ``"alias"`` / ``"normalize"`` /
            ``"embedding"``).
        score: tier 별 score. exact/alias=1.0, normalize=0.9, embedding=cosine.
    """

    retrieved_index: int
    tier: str
    score: float


@dataclass
class MatchReport:
    """한 GoldItem 의 전체 매칭 보고.

    매칭된 retrieved entity 들의 (name.lower, type) 키를 rank 순서대로
    노출하여 generic ``metrics.recall_at_k`` 등에 그대로 입력 가능하다.

    Attributes:
        results: 각 golden 의 매칭 결과 (없으면 ``None``).
        retrieved_keys_in_rank_order: rank 보존 list — 매칭된 retrieved
            entity 의 ``(name.lower, type)`` 키. 미매칭 골든은 포함되지
            않는다.
        relevant_keys: 매칭에 성공한 골든 entity 의 ``(name.lower, type)``
            키 집합 (메트릭의 relevant set).
        all_relevant_keys: **모든** 골든 entity 의 키 집합 — recall 분모
            계산에 사용. 미매칭 골든도 포함.
        surface_retrieved_keys_in_rank_order: 표면(T1/T2/T3) tier 만으로
            매칭된 retrieved entity 의 ``(name.lower, type)`` 키 — rank 보존.
            T4(embedding) 매칭은 제외. surface 메트릭의 입력.
        surface_relevant_keys: 표면 tier 로 매칭에 성공한 골든 entity 의 키
            집합. (surface recall 분자.)
        matched_retrieved_indices: 매칭에 성공한 골든들이 hit 한 **retrieved**
            엔티티의 list 인덱스 집합 (rank-0 기준). retrieved-중심 precision
            계산에 사용 — ``|matched ∩ {0..k-1}| / min(k, len(retrieved))``.
        surface_matched_retrieved_indices: 표면(T1/T2/T3) tier 만으로 매칭된
            retrieved 인덱스 집합. surface precision 의 분자.
        tier_counts: tier 별 hit 카운트.
        scores: hit 한 매칭들의 score 리스트 (평균/최소/최대 보고용).
    """

    results: list[MatchResult | None] = field(default_factory=list)
    retrieved_keys_in_rank_order: list[tuple[str, str]] = field(default_factory=list)
    relevant_keys: set[tuple[str, str]] = field(default_factory=set)
    all_relevant_keys: set[tuple[str, str]] = field(default_factory=set)
    surface_retrieved_keys_in_rank_order: list[tuple[str, str]] = field(
        default_factory=list,
    )
    surface_relevant_keys: set[tuple[str, str]] = field(default_factory=set)
    matched_retrieved_indices: set[int] = field(default_factory=set)
    surface_matched_retrieved_indices: set[int] = field(default_factory=set)
    tier_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(MATCH_TIERS, 0))
    scores: list[float] = field(default_factory=list)

    def avg_score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores) / len(self.scores)

    def min_score(self) -> float:
        return min(self.scores) if self.scores else 0.0

    def max_score(self) -> float:
        return max(self.scores) if self.scores else 0.0


# ---------------------------------------------------------------------------
# Embedding wrapper (캐시 + 동기 어댑터)
# ---------------------------------------------------------------------------


EmbedFn = Callable[[str], list[float] | None]
"""문자열 → 임베딩 벡터 (또는 ``None`` 시 T4 skip).

평가 코드에서는 :func:`build_embed_fn` 으로 비동기 임베딩 클라이언트를
싱크 + LRU 캐시 래퍼로 감싸 주입한다.
"""


def build_embed_fn(
    embedding_client: Any,
    *,
    cache_size: int = 1024,
    model_id: str = "",
) -> EmbedFn:
    """비동기 임베딩 클라이언트를 동기 + 캐시 래퍼로 변환한다.

    한 평가 실행 내에서 같은 텍스트는 1회만 임베딩 호출이 발생하도록 LRU
    캐시를 적용한다. ``embedding_client`` 가 ``None`` 이면 항상 ``None`` 을
    반환하는 함수를 돌려준다 (T4 skip).

    반환된 callable 에는 두 가지 부가 속성이 부착된다 — 호출부가 T4 단계
    skip 여부를 명시적으로 식별할 수 있게 한다:

    * ``t4_disabled`` (bool): 임베딩 클라이언트가 없거나, 이벤트 루프
      충돌로 async 경로를 쓸 수 없는 상황을 한 번이라도 만나면 ``True``.
    * ``skip_count`` (int): T4 임베딩 시도가 ``None`` 으로 떨어진 횟수.

    Args:
        embedding_client: langchain ``Embeddings`` 인터페이스 — 동기
            ``embed_query`` 또는 비동기 ``aembed_query`` 를 가져야 한다.
        cache_size: LRU 캐시 최대 항목 수.
        model_id: 캐시 키에 들어갈 모델 ID. 평가 실행마다 다른 모델이
            쓰일 수 있으므로 함수 외부에서 명시.

    Returns:
        ``text -> list[float] | None`` 동기 함수 (속성 부착).
    """
    if embedding_client is None:
        def _disabled(_t: str) -> list[float] | None:
            _disabled.skip_count += 1  # type: ignore[attr-defined]
            return None
        _disabled.t4_disabled = True  # type: ignore[attr-defined]
        _disabled.skip_count = 0  # type: ignore[attr-defined]
        return _disabled

    import asyncio

    state = {"t4_disabled": False, "skip_count": 0}

    def _call(text: str) -> list[float] | None:
        if not text:
            return None
        # 동기 호출이 가능하면 우선 사용.
        if hasattr(embedding_client, "embed_query"):
            try:
                return list(embedding_client.embed_query(text))
            except Exception:
                logger.debug("동기 embed_query 실패, async 폴백", exc_info=True)
        if hasattr(embedding_client, "aembed_query"):
            try:
                coro = embedding_client.aembed_query(text)
                try:
                    asyncio.get_running_loop()
                    # 이미 이벤트 루프가 도는 중이면 nest 불가 — 동기 메서드를
                    # 다시 시도. 임베딩 클라이언트가 동기 메서드를 가지면 위에서
                    # 이미 처리됨. 운영 평가에서는 동기 컨텍스트에서 호출된다.
                    logger.warning(
                        "이벤트 루프 내에서 비동기 임베딩을 동기로 호출할 수 없음. "
                        "임베딩 클라이언트의 embed_query 를 사용하세요.",
                    )
                    state["t4_disabled"] = True
                    return None
                except RuntimeError:
                    return list(asyncio.run(coro))
            except Exception:
                logger.warning("embedding 호출 실패", exc_info=True)
        return None

    cache: dict[tuple[str, str], list[float] | None] = {}
    order: list[tuple[str, str]] = []

    def _cached(text: str) -> list[float] | None:
        key = (model_id, text)
        if key in cache:
            return cache[key]
        emb = _call(text)
        cache[key] = emb
        order.append(key)
        if len(order) > cache_size:
            evict = order.pop(0)
            cache.pop(evict, None)
        if emb is None and text:
            _cached.skip_count += 1  # type: ignore[attr-defined]
            if state["t4_disabled"]:
                _cached.t4_disabled = True  # type: ignore[attr-defined]
        return emb

    _cached.t4_disabled = False  # type: ignore[attr-defined]
    _cached.skip_count = 0  # type: ignore[attr-defined]
    return _cached


# ---------------------------------------------------------------------------
# Entity matching (4-tier cascade)
# ---------------------------------------------------------------------------


def match_entity_tiered(
    golden: GraphEntityRef,
    retrieved: list[GraphEntityRef],
    embed_fn: EmbedFn,
    *,
    threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    strict: bool = False,
) -> MatchResult | None:
    """한 골든 엔티티에 대해 4-tier cascade 매칭을 수행한다.

    Args:
        golden: 골드셋의 정답 엔티티 (description / aliases 등 보유 가능).
        retrieved: 검색 결과 엔티티 리스트 (rank 순서 보존).
        embed_fn: 텍스트 → 임베딩 벡터 함수 (캐시 권장). T4 에서만 호출.
        threshold: T4 cosine 임계값.
        strict: True 면 T1 만 발동 (1차 동작 호환). T2/T3/T4 skip.

    Returns:
        hit 시 :class:`MatchResult`. 미매칭 시 ``None``.
    """
    g_name = (golden.name or "").strip()
    g_type = (golden.type or "").strip()
    g_name_lower = g_name.lower()

    # T1 — exact
    for i, r in enumerate(retrieved):
        r_name = (r.name or "").strip().lower()
        r_type = (r.type or "").strip()
        if r_name == g_name_lower and r_type == g_type:
            return MatchResult(retrieved_index=i, tier="exact", score=1.0)

    if strict:
        return None

    # T2 — alias OR (type 정확)
    if golden.aliases:
        alias_lowers = {(a or "").strip().lower() for a in golden.aliases}
        alias_lowers.discard("")
        if alias_lowers:
            for i, r in enumerate(retrieved):
                r_name = (r.name or "").strip().lower()
                r_type = (r.type or "").strip()
                if r_name in alias_lowers and r_type == g_type:
                    return MatchResult(retrieved_index=i, tier="alias", score=1.0)

    # T3 — normalize (NFKC + 공백·구두점 제거, type 정확)
    g_norm = _normalize(g_name)
    if g_norm:
        for i, r in enumerate(retrieved):
            r_name = (r.name or "").strip()
            r_type = (r.type or "").strip()
            if _normalize(r_name) == g_norm and r_type == g_type:
                return MatchResult(retrieved_index=i, tier="normalize", score=0.9)

    # T4 — embedding (type-agnostic).
    # 골든 description 이 비어 있으면 name 으로 fallback 한다 — 합성 골드셋이
    # description 을 항상 채우지 못하므로 T1~T3 표면 매칭 실패한 골든이 T4 에서도
    # 자동 skip 되어 미매칭으로 누락되는 것을 방지. 검색 측의 r_text fallback
    # (name 사용) 과 대칭.
    g_text = golden.description or golden.name or ""
    if not g_text:
        return None

    g_emb = golden.description_embedding
    if g_emb is None:
        g_emb = embed_fn(g_text)
    if not g_emb:
        return None

    best: MatchResult | None = None
    for i, r in enumerate(retrieved):
        r_text = (r.description or "").strip() or (r.name or "").strip()
        if not r_text:
            continue
        # retrieved 엔티티는 description_embedding 미보유 (검색 측 패스스루
        # 비용 최소화). lazy 계산.
        r_emb = embed_fn(r_text)
        if not r_emb:
            continue
        sim = cosine_similarity(g_emb, r_emb)
        if sim >= threshold and (best is None or sim > best.score):
            best = MatchResult(retrieved_index=i, tier="embedding", score=sim)
    return best


# ---------------------------------------------------------------------------
# Report 빌드 — evaluate_one 에서 호출
# ---------------------------------------------------------------------------


def run_entity_matching(
    relevant: list[GraphEntityRef],
    retrieved: list[GraphEntityRef],
    *,
    embed_fn: EmbedFn,
    threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    strict: bool = False,
) -> MatchReport:
    """골든 엔티티 전체에 대해 매칭을 수행하고 메트릭용 보고를 만든다.

    반환된 :class:`MatchReport` 의 ``retrieved_keys_in_rank_order`` /
    ``relevant_keys`` 를 :func:`context_loop.eval.metrics.recall_at_k` 등에
    그대로 입력해 메트릭을 계산한다.

    미매칭 골든은 ``relevant_keys`` 에 들어가지 않으므로 recall 의 분자에
    빠진다. recall 분모는 ``all_relevant_keys`` (= ``relevant`` 의 모든 키)
    이며, 둘 다 메트릭 함수에 전달된다.

    Args:
        relevant: 골드셋의 정답 엔티티 리스트.
        retrieved: 검색 결과 엔티티 리스트 (rank 순서 보존).
        embed_fn: 텍스트 → 임베딩 함수.
        threshold: T4 cosine 임계값.
        strict: True 면 T1 만 발동.
    """
    report = MatchReport()
    report.results = [None] * len(relevant)

    # all_relevant_keys 는 골든 측의 (name.lower, type) 모두.
    for g in relevant:
        report.all_relevant_keys.add(
            ((g.name or "").strip().lower(), (g.type or "").strip()),
        )

    # rank 보존을 위해 retrieved 의 등장 순서대로 hit 을 정렬 — 결과 list 의
    # 각 항목은 어떤 golden 이 어떤 rank 의 retrieved 와 매칭됐는지 기록.
    hits: list[tuple[int, int, MatchResult]] = []  # (retrieved_index, golden_index, MatchResult)
    for gi, g in enumerate(relevant):
        result = match_entity_tiered(
            g, retrieved, embed_fn, threshold=threshold, strict=strict,
        )
        report.results[gi] = result
        if result is not None:
            hits.append((result.retrieved_index, gi, result))
            report.tier_counts[result.tier] = report.tier_counts.get(result.tier, 0) + 1
            report.scores.append(result.score)
            g_key = ((g.name or "").strip().lower(), (g.type or "").strip())
            report.relevant_keys.add(g_key)
            # retrieved-중심 precision 용 인덱스 집계 (S1-1) — 같은 패스에서 수집.
            report.matched_retrieved_indices.add(result.retrieved_index)
            if result.tier != "embedding":
                report.surface_relevant_keys.add(g_key)
                report.surface_matched_retrieved_indices.add(result.retrieved_index)

    # 매칭된 retrieved 키를 rank 순서로 정렬 — generic 메트릭의 입력.
    # 동일 패스에서 표면(T1/T2/T3) tier 만 따로 모아 surface 키도 채운다 —
    # T4 임베딩 매칭 비용을 추가로 들이지 않는다.
    hits.sort(key=lambda t: t[0])
    seen: set[tuple[str, str]] = set()
    surface_seen: set[tuple[str, str]] = set()
    for _r_idx, g_idx, result in hits:
        g = relevant[g_idx]
        key = ((g.name or "").strip().lower(), (g.type or "").strip())
        if key not in seen:
            seen.add(key)
            report.retrieved_keys_in_rank_order.append(key)
        if result.tier != "embedding" and key not in surface_seen:
            surface_seen.add(key)
            report.surface_retrieved_keys_in_rank_order.append(key)

    return report


# ---------------------------------------------------------------------------
# Relation matching (옵셔널 — --score-relations)
# ---------------------------------------------------------------------------


RelKey = tuple[str, str, str]


def _rel_key(rel: GraphRelationRef) -> RelKey:
    return (
        (rel.source_name or "").strip().lower(),
        (rel.target_name or "").strip().lower(),
        (rel.relation_type or "").strip(),
    )


def match_relation_tiered(
    golden: GraphRelationRef,
    retrieved: list[GraphRelationRef],
    embed_fn: EmbedFn,
    *,
    threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    strict: bool = False,
) -> MatchResult | None:
    """관계 매칭 — T1 (exact key) → T4 (description embedding, type-agnostic)."""
    g_key = _rel_key(golden)

    # T1 — exact (source, target, relation_type)
    for i, r in enumerate(retrieved):
        if _rel_key(r) == g_key:
            return MatchResult(retrieved_index=i, tier="exact", score=1.0)

    if strict:
        return None

    # T4 — embedding (relation_type 무시; source/target 만 lower 비교).
    if not golden.description:
        return None
    g_emb = golden.description_embedding
    if g_emb is None:
        g_emb = embed_fn(golden.description)
    if not g_emb:
        return None

    g_src = g_key[0]
    g_tgt = g_key[1]
    best: MatchResult | None = None
    for i, r in enumerate(retrieved):
        if (r.source_name or "").strip().lower() != g_src:
            continue
        if (r.target_name or "").strip().lower() != g_tgt:
            continue
        r_text = (r.description or "").strip() or (r.relation_type or "").strip()
        if not r_text:
            continue
        r_emb = embed_fn(r_text)
        if not r_emb:
            continue
        sim = cosine_similarity(g_emb, r_emb)
        if sim >= threshold and (best is None or sim > best.score):
            best = MatchResult(retrieved_index=i, tier="embedding", score=sim)
    return best


@dataclass
class RelationMatchReport:
    """관계 매칭 보고 — 엔티티 :class:`MatchReport` 의 관계 버전 (S1-1).

    Attributes:
        retrieved_keys_in_rank_order: 매칭된 관계 키를 rank 순서로.
        relevant_keys: 매칭 성공한 골든 관계 키 집합.
        tier_counts: tier 별 hit 카운트.
        scores: hit score 리스트.
        matched_retrieved_indices: 매칭된 골든들이 hit 한 retrieved 관계의
            list 인덱스 집합. retrieved-중심 relation precision 분자에 사용.
    """

    retrieved_keys_in_rank_order: list[RelKey] = field(default_factory=list)
    relevant_keys: set[RelKey] = field(default_factory=set)
    tier_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(MATCH_TIERS, 0),
    )
    scores: list[float] = field(default_factory=list)
    matched_retrieved_indices: set[int] = field(default_factory=set)


def run_relation_matching(
    relevant: list[GraphRelationRef],
    retrieved: list[GraphRelationRef],
    *,
    embed_fn: EmbedFn,
    threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    strict: bool = False,
) -> RelationMatchReport:
    """관계 매칭 보고. :class:`RelationMatchReport` 반환.

    엔티티 매칭과 같은 list/set 패턴을 따라 ``metrics.recall_at_k`` 등에
    바로 사용 가능하다. ``matched_retrieved_indices`` 는 S1-1 의 retrieved-중심
    relation precision 계산에 쓰인다 — 엔티티와 동일하게 같은 패스에서 수집해
    T4 추가 비용이 없다.
    """
    report = RelationMatchReport()
    hits: list[tuple[int, RelKey, MatchResult]] = []
    for gi, g in enumerate(relevant):
        result = match_relation_tiered(
            g, retrieved, embed_fn, threshold=threshold, strict=strict,
        )
        if result is None:
            continue
        key = _rel_key(g)
        report.relevant_keys.add(key)
        report.tier_counts[result.tier] = report.tier_counts.get(result.tier, 0) + 1
        report.scores.append(result.score)
        report.matched_retrieved_indices.add(result.retrieved_index)
        hits.append((result.retrieved_index, key, result))
        _ = gi  # 결정성 디버깅용

    hits.sort(key=lambda t: t[0])
    seen: set[RelKey] = set()
    for _idx, key, _res in hits:
        if key in seen:
            continue
        seen.add(key)
        report.retrieved_keys_in_rank_order.append(key)
    return report


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def aggregate_tier_counts(rows_tier_counts: list[dict[str, int]]) -> dict[str, int]:
    """여러 쿼리의 tier 카운트 dict 를 단순 합산한다.

    ``write_summary`` 의 ``metrics_by_mode.graph`` 보고에 사용. 골드셋
    전체에서 tier 별 hit 수의 누적 분포를 가시화한다.
    """
    total: dict[str, int] = dict.fromkeys(MATCH_TIERS, 0)
    for row in rows_tier_counts:
        for tier in MATCH_TIERS:
            total[tier] += int(row.get(tier, 0))
    return total


# 임베딩 비동기 헬퍼 — build_synthetic_gold_set 의 동기 호출 경로용.


async def aembed_with_client(
    embedding_client: Any,
    texts: list[str],
) -> list[list[float] | None]:
    """비동기 임베딩 클라이언트로 텍스트 배치를 임베딩한다.

    빌드 스크립트에서 graph evidence description 들을 1회에 모아 임베딩할 때
    사용. ``embedding_client`` 가 ``None`` 이면 모두 ``None`` 반환.

    각 텍스트별로 성공/실패를 독립 처리하여, 일부 실패해도 전체가 죽지 않도록
    한다. 빈 문자열은 항상 ``None``.

    Args:
        embedding_client: langchain ``Embeddings`` 호환 클라이언트.
        texts: 임베딩할 문자열 리스트.

    Returns:
        각 입력에 대응되는 임베딩(또는 None) 리스트. 길이 = ``len(texts)``.
    """
    if embedding_client is None:
        return [None] * len(texts)
    nonempty_indices = [i for i, t in enumerate(texts) if t]
    nonempty_texts = [texts[i] for i in nonempty_indices]
    if not nonempty_texts:
        return [None] * len(texts)
    try:
        embeddings = await embedding_client.aembed_documents(nonempty_texts)
    except Exception:
        logger.warning("배치 임베딩 실패", exc_info=True)
        return [None] * len(texts)
    out: list[list[float] | None] = [None] * len(texts)
    for i, emb in zip(nonempty_indices, embeddings):
        out[i] = list(emb)
    return out


__all__ = [
    "DEFAULT_GRAPH_MATCH_THRESHOLD",
    "MATCH_TIERS",
    "EmbedFn",
    "MatchReport",
    "MatchResult",
    "RelationMatchReport",
    "aembed_with_client",
    "aggregate_tier_counts",
    "build_embed_fn",
    "cosine_similarity",
    "match_entity_tiered",
    "match_relation_tiered",
    "run_entity_matching",
    "run_relation_matching",
]
