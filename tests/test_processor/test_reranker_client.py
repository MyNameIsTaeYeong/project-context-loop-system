"""전용 리랭커 클라이언트 응답 파싱 테스트."""

from __future__ import annotations

import pytest

from context_loop.processor.reranker_client import parse_rerank_response


def test_parse_cohere_jina_format() -> None:
    """Cohere/Jina 형식: ``{"results": [{"index", "relevance_score"}]}``."""
    data = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.4},
        ]
    }
    assert parse_rerank_response(data, 2) == [0.9, 0.4]


def test_parse_tei_format() -> None:
    """TEI 형식: ``[{"index", "score"}]`` (top-level list)."""
    data = [
        {"index": 1, "score": 0.7},
        {"index": 0, "score": 0.3},
    ]
    assert parse_rerank_response(data, 2) == [0.3, 0.7]


def test_parse_ordered_scores_dict() -> None:
    """단순 점수 배열 (dict): ``{"scores": [0.9, 0.4]}``."""
    data = {"scores": [0.9, 0.4]}
    assert parse_rerank_response(data, 2) == [0.9, 0.4]


def test_parse_ordered_scores_list() -> None:
    """단순 점수 배열 (top-level list): ``[0.9, 0.4]``."""
    data = [0.9, 0.4]
    assert parse_rerank_response(data, 2) == [0.9, 0.4]


def test_parse_missing_indices_padded_with_zero() -> None:
    """응답에 누락된 인덱스는 0.0으로 채워진다."""
    data = {"results": [{"index": 1, "relevance_score": 0.7}]}
    assert parse_rerank_response(data, 3) == [0.0, 0.7, 0.0]


def test_parse_out_of_range_indices_ignored() -> None:
    """범위 밖 인덱스는 무시된다."""
    data = {
        "results": [
            {"index": 0, "relevance_score": 0.5},
            {"index": 5, "relevance_score": 0.9},
        ]
    }
    assert parse_rerank_response(data, 2) == [0.5, 0.0]


def test_parse_short_ordered_scores_padded() -> None:
    """ordered 형식에서 길이가 부족하면 0.0으로 채운다."""
    assert parse_rerank_response([0.7], 3) == [0.7, 0.0, 0.0]


def test_parse_unknown_dict_format_raises() -> None:
    """results/scores 키가 없는 dict 응답은 예외를 발생시킨다."""
    with pytest.raises(ValueError):
        parse_rerank_response({"unexpected": "format"}, 2)


def test_parse_invalid_top_level_raises() -> None:
    """list/dict 가 아닌 응답은 예외를 발생시킨다."""
    with pytest.raises(ValueError):
        parse_rerank_response("invalid", 2)
