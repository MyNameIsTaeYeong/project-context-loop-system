"""엔티티 이름 정규화 (룰 기반, source-agnostic).

R3: 그래프 노드 머지 키를 ``entity_name`` 원본 → 정규화된 키로 교체하기 위한
공통 유틸. 설계서: ``_workspace/indexing-improvement/R2_semantic_merge_review.md``
§3.2 D, §5.1 단기 채택 항목.

정규화 규칙(순서대로 적용):
1. Unicode NFKC 정규화 (전각/반각, 호환 합자 등 통일)
2. 양끝 공백 strip
3. 연속 공백 → 단일 공백
4. 모든 공백 / ``-`` / ``_`` 제거 (한 가지 정책으로 통일 — 빈 문자열로 join)
5. 케이스 폴딩 (``str.lower``)

본 라운드에서 채택하지 않는 규칙:
- 양 끝 괄호 묶음 제거 (``(v2)``/``(legacy)``) — 사례 3 의 false-merge 위험.
- 한자/일본어 변환 — false-merge 위험.

특성:
- **deterministic**: 같은 입력 → 항상 같은 키. idempotency 자명 보장.
- **빈/None 안전**: ``""`` 반환.
- **source-agnostic**: confluence_mcp / git_code 등 모든 소스에서 재사용.
"""

from __future__ import annotations

import re
import unicodedata

# 4번 규칙 — 공백/하이픈/언더스코어를 모두 제거. 한 가지 정책으로 통일하기 위해
# 빈 문자열로 join 한다. (설계서: "권장: 모두 제거하여 빈 문자열로 join")
#
# NOTE: unicode whitespace 전체를 대상으로 하기 위해 ``\s`` 를 사용한다.
# 별도로 ``-`` (HYPHEN-MINUS) 와 ``_`` (LOW LINE) 를 명시. NFKC 가 호환 분해를
# 수행하므로 ``－`` (FULLWIDTH HYPHEN-MINUS) 등은 이미 ``-`` 로 통일된다.
_STRIP_CHARS_PATTERN = re.compile(r"[\s\-_]+")
_MULTI_SPACE_PATTERN = re.compile(r"\s+")


def normalize_entity_name(name: str | None) -> str:
    """엔티티 이름을 정규화된 키로 변환한다.

    Args:
        name: 원본 엔티티 이름. ``None`` 또는 빈 문자열도 안전하게 처리.

    Returns:
        정규화된 키 문자열. 입력이 ``None``/``""`` 또는 정규화 결과가 빈 경우
        ``""`` 를 반환한다.

    Examples:
        >>> normalize_entity_name("Payment Service")
        'paymentservice'
        >>> normalize_entity_name("결제 시스템")
        '결제시스템'
        >>> normalize_entity_name("결제-시스템")
        '결제시스템'
        >>> normalize_entity_name("auth_service")
        'authservice'
        >>> normalize_entity_name("  ")
        ''
        >>> normalize_entity_name(None)
        ''
    """
    if not name:
        return ""

    # 1. NFKC 정규화
    text = unicodedata.normalize("NFKC", name)

    # 2. 양끝 공백 제거
    text = text.strip()
    if not text:
        return ""

    # 3. 연속 공백 → 단일 공백 (시각적 결과는 4번에서 모두 제거되므로 의미적
    #    효과는 없으나, 4번 정책 변경 시 안전한 베이스를 보장하기 위해 유지).
    text = _MULTI_SPACE_PATTERN.sub(" ", text)

    # 4. 공백 / ``-`` / ``_`` 모두 제거 (빈 문자열로 join)
    text = _STRIP_CHARS_PATTERN.sub("", text)

    # 5. 케이스 폴딩
    return text.lower()
