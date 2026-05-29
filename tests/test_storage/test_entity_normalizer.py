"""``entity_normalizer.normalize_entity_name`` 단위 테스트.

R3 채택 후보 D (룰 기반 정규화) 의 정규화 사례표
(``_workspace/indexing-improvement/R2_semantic_merge_review.md`` §8) 를 검증.
"""

from __future__ import annotations

import pytest

from context_loop.storage.entity_normalizer import normalize_entity_name


class TestNormalizeEntityName:
    """설계서 §8 부록의 8개 사례 + 빈/None 안전."""

    def test_case_folding_simple_english(self) -> None:
        # `Payment Service` vs `payment service` — 케이스 폴딩 + 공백 제거
        assert normalize_entity_name("Payment Service") == normalize_entity_name(
            "payment service",
        )

    def test_korean_whitespace_squeeze(self) -> None:
        # `결제 시스템` vs `결제시스템` — 공백 제거 후 동일
        assert normalize_entity_name("결제 시스템") == normalize_entity_name(
            "결제시스템",
        )

    def test_korean_dash_separator(self) -> None:
        # `결제 시스템` vs `결제-시스템` — dash 제거 후 동일
        assert normalize_entity_name("결제 시스템") == normalize_entity_name(
            "결제-시스템",
        )

    def test_abbrev_vs_fullname_not_merged(self) -> None:
        # `PG` vs `Payment Gateway` — D 만으로 불가 (설계서 §8 사례 4)
        assert normalize_entity_name("PG") != normalize_entity_name(
            "Payment Gateway",
        )

    def test_multilingual_not_merged(self) -> None:
        # `결제 서비스` vs `Payment Service` — D 만으로 불가 (설계서 §8 사례 1)
        assert normalize_entity_name("결제 서비스") != normalize_entity_name(
            "Payment Service",
        )

    def test_version_parentheses_preserved(self) -> None:
        # `결제 시스템` vs `결제 시스템(v2)` — R3 에서 괄호 제거 미채택. 별개 유지.
        # (설계서 §3.2 D 의 5번 괄호 제거 규칙 의도적 제외)
        assert normalize_entity_name("결제 시스템") != normalize_entity_name(
            "결제 시스템(v2)",
        )

    def test_legacy_parentheses_preserved(self) -> None:
        # 동일하게 ` (legacy)` 같은 부가표기도 D 단계에선 별개 노드로 유지
        assert normalize_entity_name("결제 시스템") != normalize_entity_name(
            "결제 시스템 (legacy)",
        )

    def test_same_entity_type_homonym_still_collides(self) -> None:
        # 사례 5 — 동음이의어는 D 가 본질적으로 해결 못함 (정규화 키는 같다).
        # 본 테스트는 D 의 한계를 명시적으로 가드.
        assert normalize_entity_name("API") == normalize_entity_name("API")

    def test_underscore_separator(self) -> None:
        # 추가 가드: `auth_service` vs `auth-service` vs `Auth Service` 동일
        assert (
            normalize_entity_name("auth_service")
            == normalize_entity_name("auth-service")
            == normalize_entity_name("Auth Service")
            == "authservice"
        )

    def test_nfkc_fullwidth_normalization(self) -> None:
        # 전각 영문 / 전각 공백 → NFKC 로 통일
        # `ＰＡＹＭＥＮＴ` (fullwidth) → `payment`
        assert normalize_entity_name("ＰＡＹＭＥＮＴ") == "payment"

    def test_squeeze_consecutive_whitespace(self) -> None:
        # `결제   시스템` (연속 공백) — strip + squeeze + 공백 제거
        assert normalize_entity_name("결제   시스템") == normalize_entity_name(
            "결제 시스템",
        )

    def test_strip_outer_whitespace(self) -> None:
        # 양끝 공백 제거
        assert normalize_entity_name("  결제 시스템  ") == normalize_entity_name(
            "결제 시스템",
        )

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "\t\n", "  --  __  "],
    )
    def test_empty_inputs(self, value: str | None) -> None:
        # None / 빈 문자열 / whitespace 만 / 구분자만 → 모두 빈 문자열
        assert normalize_entity_name(value) == ""

    def test_deterministic_idempotent(self) -> None:
        # 같은 입력 → 항상 같은 출력 (idempotency 회귀 가드)
        inputs = ["Payment Service", "결제 시스템", "auth_service"]
        for s in inputs:
            assert normalize_entity_name(s) == normalize_entity_name(s)
            # 정규화된 키를 다시 정규화해도 동일 (멱등성)
            assert normalize_entity_name(normalize_entity_name(s)) == normalize_entity_name(s)
