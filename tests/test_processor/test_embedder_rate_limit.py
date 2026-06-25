"""EndpointEmbeddingClient의 429 재시도/백오프 및 전역 동시성 상한 테스트."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from context_loop.processor import embedder
from context_loop.processor.embedder import EndpointEmbeddingClient

_URL = "http://embed.test/v1/embeddings"
_REAL_SLEEP = asyncio.sleep  # autouse no_sleep 패치 전 원본 참조를 보관


def _ok_response(url: str, num_inputs: int) -> httpx.Response:
    """입력 개수만큼 더미 임베딩을 담은 200 응답을 만든다."""
    data = [{"index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(num_inputs)]
    return httpx.Response(200, json={"data": data}, request=httpx.Request("POST", url))


def _err_response(url: str, status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", url))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """asyncio.sleep을 무력화하고 호출된 지연값을 기록한다 (테스트 지연 방지)."""
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(embedder.asyncio, "sleep", _fake_sleep)
    return recorded


def _patch_post(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> dict[str, int]:
    """AsyncClient.post를 순차 응답 목록으로 교체한다. 마지막 응답은 반복 사용된다."""
    state = {"calls": 0}

    async def _mock_post(self, url, *, json, headers):  # noqa: ANN001
        idx = min(state["calls"], len(responses) - 1)
        resp = responses[idx]
        state["calls"] += 1
        # 200 응답이면 실제 입력 개수에 맞춘 새 응답으로 대체한다.
        if resp.status_code == 200:
            return _ok_response(url, len(json["input"]))
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)
    return state


async def test_aembed_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """첫 응답이 429여도 재시도 후 200을 받으면 정상 결과를 반환한다."""
    state = _patch_post(
        monkeypatch,
        [_err_response(_URL, 429), _ok_response(_URL, 1)],
    )
    client = EndpointEmbeddingClient(_URL, "m", backoff_base=0.0)
    result = await client.aembed_documents(["hello"])
    assert len(result) == 1
    assert state["calls"] == 2


async def test_aembed_respects_retry_after_header(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """429 응답의 Retry-After 헤더 값이 백오프 대기에 우선 적용된다."""
    _patch_post(
        monkeypatch,
        [_err_response(_URL, 429, {"Retry-After": "7"}), _ok_response(_URL, 1)],
    )
    client = EndpointEmbeddingClient(_URL, "m", backoff_base=2.0)
    await client.aembed_documents(["hello"])
    assert _no_sleep == [7.0]


async def test_aembed_raises_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """계속 429면 max_retries회 시도 후 예외를 전파한다."""
    state = _patch_post(monkeypatch, [_err_response(_URL, 429)])
    client = EndpointEmbeddingClient(_URL, "m", max_retries=3, backoff_base=0.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.aembed_documents(["hello"])
    assert state["calls"] == 3


async def test_aembed_non_retryable_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 같은 비재시도 오류는 재시도 없이 즉시 예외를 던진다."""
    state = _patch_post(monkeypatch, [_err_response(_URL, 401)])
    client = EndpointEmbeddingClient(_URL, "m", max_retries=5, backoff_base=0.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.aembed_documents(["hello"])
    assert state["calls"] == 1


async def test_global_concurrency_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """공유 세마포어가 동시 in-flight 요청 수를 max_concurrency로 제한한다."""
    tracker = {"active": 0, "max_active": 0}

    async def _mock_post(self, url, *, json, headers):  # noqa: ANN001
        tracker["active"] += 1
        tracker["max_active"] = max(tracker["max_active"], tracker["active"])
        await _REAL_SLEEP(0.01)  # 실제로 양보해 동시 in-flight 상태를 관찰 가능하게 함
        tracker["active"] -= 1
        return _ok_response(url, len(json["input"]))

    monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)

    client = EndpointEmbeddingClient(_URL, "m", max_concurrency=2)
    await asyncio.gather(*(client.aembed_query(f"q{i}") for i in range(10)))
    assert tracker["max_active"] <= 2
