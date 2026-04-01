"""llm_client extract_json 테스트."""

from __future__ import annotations

import json

import pytest

from context_loop.processor.llm_client import extract_json


class TestExtractJson:
    """extract_json 함수 테스트."""

    def test_plain_json(self) -> None:
        """순수 JSON 문자열에서 추출한다."""
        data = {"key": "value"}
        assert extract_json(json.dumps(data)) == data

    def test_markdown_code_block(self) -> None:
        """마크다운 코드 블록에서 JSON을 추출한다."""
        text = '```json\n{"key": "value"}\n```'
        assert extract_json(text) == {"key": "value"}

    def test_think_tag_removal(self) -> None:
        """<think>...</think> 블록을 제거하고 JSON을 추출한다."""
        text = (
            "<think>\n이 질의는 Gateway 엔티티와 관련이 있으므로...\n"
            "탐색이 필요합니다.\n</think>\n"
            '```json\n{"should_search": true, "reasoning": "테스트", "search_steps": []}\n```'
        )
        result = extract_json(text)
        assert result["should_search"] is True
        assert result["reasoning"] == "테스트"

    def test_think_tag_with_plain_json(self) -> None:
        """<think> 태그 후 코드 블록 없이 순수 JSON이 오는 경우."""
        text = (
            "<think>추론 내용</think>\n"
            '{"should_search": false, "reasoning": "불필요", "search_steps": []}'
        )
        result = extract_json(text)
        assert result["should_search"] is False

    def test_multiple_think_tags(self) -> None:
        """여러 개의 <think> 블록이 있어도 모두 제거한다."""
        text = (
            "<think>첫 번째 추론</think>\n"
            "<think>두 번째 추론</think>\n"
            '{"key": 123}'
        )
        assert extract_json(text) == {"key": 123}

    def test_no_json_raises(self) -> None:
        """JSON이 없으면 ValueError를 발생시킨다."""
        with pytest.raises(ValueError):
            extract_json("no json here")

    def test_think_only_raises(self) -> None:
        """<think> 블록만 있고 JSON이 없으면 ValueError를 발생시킨다."""
        with pytest.raises(ValueError):
            extract_json("<think>추론만 있음</think>")
