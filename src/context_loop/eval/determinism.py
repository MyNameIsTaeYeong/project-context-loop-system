"""평가 재현성을 위한 결정적 seed 유틸.

LLM 호출(Judge)에 전달할 seed 를 입력 텍스트로부터
**프로세스에 독립적으로** 유도한다.

주의: Python 내장 ``hash(str)`` 은 ``PYTHONHASHSEED`` 로 프로세스마다 salt 되어
실행 간 값이 달라진다. 평가 재현성(같은 입력 → 같은 점수)을 위해서는 반드시
``hash()`` 대신 본 모듈의 ``stable_seed`` 를 사용해야 한다.
"""

from __future__ import annotations

import hashlib

# 엔드포인트 seed 는 32-bit 정수를 기대하는 경우가 많아 안전 범위로 환원.
_SEED_MODULO = 10_000_000


def stable_seed(text: str, base: int) -> int:
    """텍스트로부터 프로세스 독립적 결정적 seed 를 유도한다.

    Args:
        text: seed 의 근거가 되는 식별 텍스트(예: GoldItem.id, query).
        base: seed 베이스 오프셋(실행/역할 구분용).

    Returns:
        ``base + (sha256(text) % 10_000_000)``.
    """
    digest = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
    return base + (digest % _SEED_MODULO)
