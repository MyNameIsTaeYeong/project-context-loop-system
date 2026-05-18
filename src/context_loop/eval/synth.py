"""LLM 기반 골드셋 합성 — 프롬프트 빌더와 품질 게이트.

원리:
1. 청크를 보여주고 Generator LLM 에 N개 질문 생성 요청 (역방향 생성).
2. 4 단계 품질 게이트 적용:
   (a) 답변 가능성 — 출처 청크만 보고 답할 수 있는가? (Judge LLM)
   (b) 식별자 누출 — 청크 고유 식별자가 질문에 그대로 들어갔는가? (결정론)
   (c) 지시대명사·포인터 — "이 클래스/위 코드/this method" 처럼 청크 포인터를
       가정하는가? (결정론)
   (d) 일반성 — 무관한 다른 청크로도 답할 수 있는가? (Judge LLM)
3. 통과한 (질문, 청크) 페어만 골드셋에 등재.

자동 채점 시 BM25 만으로 100% 맞히는 사기성 골드셋을 막기 위해
"식별자를 베끼지 말고 의미로 풀어쓰라"는 지시가 핵심이다.

Generator 와 Judge 는 서로 다른 family 의 모델을 쓰는 것이 권장된다
(자기 평가 편향 회피).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class GeneratedQuestion:
    """생성된 질의 한 건."""

    query: str
    difficulty: str = ""  # easy | medium | hard


@dataclass
class GeneratedGraphRelation:
    """그래프 모드 LLM 출력의 관계 evidence (선택, 2차)."""

    source_name: str
    target_name: str
    relation_type: str
    description: str = ""


@dataclass
class GeneratedGraphQuestion:
    """그래프 모드 LLM 출력 — 질의 + evidence (2차).

    1차 호환: ``evidence_description`` / ``entity_aliases`` / ``relation`` 이
    누락된 LLM 응답이면 기본값으로 graceful degradation 한다.
    """

    query: str
    difficulty: str = ""
    evidence_description: str = ""
    entity_aliases: list[str] = field(default_factory=list)
    relation: GeneratedGraphRelation | None = None


@dataclass
class FilterReport:
    """품질 게이트 통과/탈락 사유 리포트."""

    passed: bool
    reason: str = ""
    """탈락 시 원인.

    가능 값:
        ``not_answerable`` — 정답 청크로 답할 수 없음 (LLM)
        ``leakage`` — ASCII 식별자 누출 (결정론)
        ``korean_leakage`` — 한국어 고유명사 누출 (결정론)
        ``demonstrative`` — 지시대명사/포인터 표현 (결정론)
        ``non_unique_source`` — 정답 청크가 유일한 출처가 아님 (LLM)
        ``generic`` — Distractor 로도 답할 수 있음 (LLM)
        ``parse_error`` — LLM 응답 파싱 실패
    """


# ---------------------------------------------------------------------------
# Prompts (모듈 변수로 노출하여 테스트/튜닝 가능)
# ---------------------------------------------------------------------------


GENERATE_PROMPT_TEMPLATE = """\
다음은 사내 문서 또는 코드의 한 청크다:

---
{chunk_content}
---

이 청크가 정답이 되도록, 사람이 자연스럽게 물어볼 만한 한국어 질문을 {n}개 생성해라.

원칙: 질문은 **검색창에 단독으로 입력해도 의미가 통해야** 한다. 사용자는
청크를 보지 않고 질문만 입력하기 때문이다.

조건:
- 한국어 자연어 질문 (의문문)
- 식별자(함수명/클래스명/변수명/페이지명)를 그대로 베끼지 말고 의미 단위로 풀어쓸 것
  ✗ 나쁜 예: "_clamp_max_per_tenant 함수가 뭔가요?"
  ○ 좋은 예: "테넌트별 최대치 제한 로직은 어떻게 동작하나요?"
- **지시대명사·포인터 표현 금지** — "이/위/아래/다음/해당/본 + 클래스/메서드/
  함수/코드/모듈/예제" 처럼 청크를 가리키는 표현은 검색 질의로 의미가
  성립하지 않으므로 사용 금지
  ✗ 나쁜 예: "이 클래스의 역할은 무엇인가요?"
  ✗ 나쁜 예: "위 코드의 동작 원리는?"
  ✗ 나쁜 예: "다음 메서드는 무엇을 반환하나요?"
  ○ 좋은 예: "결제 한도 검증 시 어떤 예외가 발생하나요?"
- 청크 안의 정보로 답할 수 있는 질문일 것 (외부 지식 요구 금지)
- 난이도를 골고루 분포: easy(도메인 사실 직접 조회) / medium(개념 이해) / hard(원인·관계·왜)

JSON 배열로만 출력해라. 다른 설명 금지:
[
  {{"q": "질문 본문", "difficulty": "easy"}},
  {{"q": "질문 본문", "difficulty": "medium"}}
]
"""


ANSWERABLE_PROMPT_TEMPLATE = """\
질문: {question}

문맥:
---
{chunk_content}
---

이 문맥만 보고 위 질문에 사실 기반으로 답할 수 있는가?
"yes" 또는 "no" 한 단어로만 답하라.
"""


GENERIC_PROMPT_TEMPLATE = """\
질문: {question}

문맥:
---
{chunk_content}
---

이 문맥이 위 질문에 대한 **유일한 정답 출처**라고 단정할 수 있는지 평가하라.

판단 기준:
- 문맥에 명시되지 않은 정보로 답해야 한다면 'no'
- 다른 일반적인 문서/매뉴얼/위키에서도 같은 답을 얻을 수 있다면 'no'
- 이 문맥에만 있는 고유한 정보로 답해야만 한다면 'yes'

yes/no 한 단어로만 답하라.
"""


GRAPH_GENERATE_PROMPT_TEMPLATE = """\
다음은 사내 지식 그래프의 한 엔티티와 그 주변 관계다:

엔티티: {entity_name} ({entity_type})
설명: {entity_description}

주변 관계:
{edges_text}

이 엔티티 또는 관계에서 답을 찾을 수 있는, 사람이 자연스럽게 물어볼 만한
한국어 질문을 {n}개 생성해라.

원칙: 질문은 **검색창에 단독으로 입력해도 의미가 통해야** 한다. 사용자는
그래프 정보를 보지 않고 질문만 입력하기 때문이다.

조건:
- 한국어 자연어 질문 (의문문)
- 엔티티 이름은 그대로 써도 되지만, **관계의 다른 엔티티 이름까지 함께 줄줄
  나열하지 말 것** — 의미 단위로 풀어쓸 것
- **지시대명사·포인터 표현 금지** — "이/위/아래/다음/해당/본 + 엔티티/관계/
  노드" 같이 그래프를 가리키는 표현은 검색 질의로 의미가 성립하지 않으므로
  사용 금지
  ✗ 나쁜 예: "이 엔티티는 무엇인가요?"
  ✗ 나쁜 예: "위 관계는 어떻게 동작?"
  ○ 좋은 예: "결제 서비스는 어느 팀이 운영하나요?"
- 그래프 정보 안에서 답할 수 있는 질문일 것 (외부 지식 요구 금지)
- 난이도를 골고루 분포: easy(엔티티 사실 조회) / medium(1-hop 관계 이해) /
  hard(2-hop 추론·왜)

각 질문에 대해 함께 보조 정보도 채워라 (선택 — 모르면 빈 값 / 빈 배열):
- ``evidence_description``: 이 질문의 정답이 되는 엔티티/관계를 1~2 문장의
  자연어로 풀어쓴 evidence. 표기/타입 명이 바뀌어도 의미 매칭에 사용된다.
- ``entity_aliases``: 같은 엔티티의 다른 표기(영문/한글/약어 등) 후보. 위
  엔티티 정보에서 자연스럽게 떠오르는 동의어만 — 추측 금지.
- ``relation``: 질문이 특정 관계를 직접 가리키면 (source / target /
  relation_type / relation_description) 으로 명시. 관계가 핵심이 아니면
  생략 가능.

JSON 배열로만 출력해라. 다른 설명 금지:
[
  {{
    "q": "질문 본문",
    "difficulty": "easy",
    "evidence_description": "결제 서비스: 주문 서비스에 의존하는 결제 처리 시스템",
    "entity_aliases": ["Payment Service", "결제서비스"],
    "relation": {{
      "source_name": "결제 서비스",
      "target_name": "주문 서비스",
      "relation_type": "depends_on",
      "relation_description": "결제 서비스는 주문 서비스에 의존한다"
    }}
  }}
]
"""


# 그래프 후보 한 건당 LLM 에 보낼 snippet 의 최대 길이 (W-1 권장 대응).
# 8000자 가량이면 약 2000~3000 토큰 — Generator 호출이 폭주하지 않는다.
GRAPH_SNIPPET_MAX_CHARS = 8000

# source_text_anchor 의 표준 prefix 길이.
ANCHOR_MAX_CHARS = 200


def _normalize_whitespace(text: str) -> str:
    """연속 공백·줄바꿈을 단일 공백으로 정규화한다."""
    return re.sub(r"\s+", " ", text).strip()


def make_text_anchor(content: str, *, max_chars: int = ANCHOR_MAX_CHARS) -> str:
    """청크 본문에서 ``max_chars`` 길이의 anchor 를 만든다.

    whitespace 를 단일 공백으로 정규화한 뒤 prefix 를 잘라낸다. 골드셋의
    ``source_text_anchor`` 필드 값으로 사용된다 (D-6 / D-8).
    """
    normalized = _normalize_whitespace(content)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars]


def build_subgraph_snippet(
    *,
    entity_name: str,
    entity_type: str,
    entity_description: str,
    edges: list[dict[str, Any]],
    max_chars: int = GRAPH_SNIPPET_MAX_CHARS,
) -> str:
    """그래프 노드 + 1-hop 엣지를 LLM 입력용 텍스트로 포맷팅한다.

    너무 긴 snippet (W-1) 을 막기 위해 ``max_chars`` 초과 시 결정론적 순서로
    edges 를 잘라낸다.
    """
    lines: list[str] = [
        f"엔티티: {entity_name} ({entity_type})",
    ]
    if entity_description:
        lines.append(f"설명: {entity_description}")
    if edges:
        lines.append("주변 관계:")
        # 결정론적 정렬 — (source, relation, target)
        sorted_edges = sorted(
            edges,
            key=lambda e: (
                str(e.get("source_name", "")),
                str(e.get("relation_type", "")),
                str(e.get("target_name", "")),
            ),
        )
        for e in sorted_edges:
            src = e.get("source_name", "?")
            tgt = e.get("target_name", "?")
            rel = e.get("relation_type", "관련")
            lines.append(f"- {src} --[{rel}]--> {tgt}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def format_edges_for_prompt(edges: list[dict[str, Any]]) -> str:
    """``GRAPH_GENERATE_PROMPT_TEMPLATE`` 의 ``edges_text`` 슬롯 포맷.

    공백 줄 없는 bullet 리스트로 단순화한다.
    """
    if not edges:
        return "(관계 없음)"
    sorted_edges = sorted(
        edges,
        key=lambda e: (
            str(e.get("source_name", "")),
            str(e.get("relation_type", "")),
            str(e.get("target_name", "")),
        ),
    )
    lines = []
    for e in sorted_edges:
        src = e.get("source_name", "?")
        tgt = e.get("target_name", "?")
        rel = e.get("relation_type", "관련")
        lines.append(f"- {src} --[{rel}]--> {tgt}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Identifier leakage detection (식별자 누출 탐지)
# ---------------------------------------------------------------------------


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
"""4자 이상의 식별자 — 너무 짧은 토큰(if, for) 은 일반어와 충돌하므로 제외."""

_COMMON_WORDS = frozenset({
    "this", "that", "with", "from", "into", "true", "false", "none", "null",
    "self", "args", "kwargs", "return", "import", "class", "function", "def",
    "type", "interface", "struct", "package", "module", "main", "test",
    "data", "config", "user", "name", "value", "result", "error",
    "string", "int", "float", "bool", "list", "dict", "object",
})


def extract_unique_tokens(text: str, *, min_len: int = 4) -> set[str]:
    """청크 본문에서 식별자성 토큰을 추출한다.

    - ASCII 식별자 (영문/숫자/_) 만 추출
    - 길이 ``min_len`` 이상
    - 일반 영단어/타입 키워드 제외
    - 케이스를 유지 (소문자화 안 함 — 케이스 유의 매칭이 누출 탐지에 유리)
    """
    tokens: set[str] = set()
    for m in _IDENT_RE.finditer(text):
        tok = m.group(0)
        if len(tok) < min_len:
            continue
        if tok.lower() in _COMMON_WORDS:
            continue
        tokens.add(tok)
    return tokens


def has_identifier_leakage(question: str, source_text: str) -> bool:
    """질문에 출처 청크의 고유 식별자가 그대로 들어 있는지 검사.

    True 면 "베껴 쓴" 질문 — 자연어 의미 매칭 능력을 평가하기에 부적합하므로
    골드셋에서 탈락시킨다.
    """
    tokens = extract_unique_tokens(source_text)
    if not tokens:
        return False
    # 질문 안에서 부분 문자열로 등장하면 누출. 단어 경계로 검사.
    for tok in tokens:
        # 대소문자 구분 (Foo 와 foo 는 다른 식별자로 취급)
        if re.search(rf"\b{re.escape(tok)}\b", question):
            return True
    return False


# ---------------------------------------------------------------------------
# Korean proper noun leakage detection (한국어 고유명사 누출 탐지)
# ---------------------------------------------------------------------------


_KOREAN_NOUN_RE = re.compile(r"[가-힣]{3,}")
"""3자 이상의 연속 한글.

S2 보강 — 4자 이상만 보던 v2 대비 3자 한글 고유명사(예: "결제팀", "주문봇") 도
포함. 일반 3자 명사 false positive 는 stem 길이 컷오프 + ``_KOREAN_COMMON_NOUNS``
화이트리스트로 통제.
"""

_KOREAN_COMMON_NOUNS = frozenset({
    # 3자 일반 명사 (S2 보강) — 빈도 높고 고유명사가 아닌 어휘
    "사용자", "관리자", "개발자", "시스템", "데이터", "서비스", "프로젝트",
    "이용자", "고객사", "회사명", "팀원들", "구성원", "담당자", "참가자",
    "사용법", "기능을", "기능이", "기능에", "기능의",
    "정보를", "정보가", "정보의", "정보는",
    "방법을", "방법이", "방법의",
    "내용을", "내용이", "내용의",
    "관련된", "다음은", "이전의", "현재의", "최근의",
    # 4자+ 일반 어휘
    "사용자가", "사용자는", "사용자의", "사용자에게",
    "관리자가", "관리자는", "관리자의",
    "프로세스", "비즈니스", "데이터베이스", "데이터셋", "데이터를",
    "애플리케이션", "인터페이스", "프레임워크", "라이브러리",
    "서비스가", "서비스는", "서비스의", "서비스를",
    "시스템이", "시스템은", "시스템의", "시스템을",
    "기능에서", "기능으로", "기능을",
    "다음과같이", "예를들면", "예를들어",
})


# 한국어 후행 조사 — 토큰 stem 추출에 사용.
_KOREAN_JOSA_RE = re.compile(
    r"(은|는|이|가|을|를|에|에서|에게|한테|으로|로|와|과|의|도|만|까지|부터|보다|마저|조차)$",
)


def _strip_korean_josa(token: str, *, min_stem_len: int = 2) -> str:
    """한국어 토큰에서 후행 조사를 1회 제거한 stem 을 반환.

    조사를 제거한 stem 의 길이가 ``min_stem_len`` 미만이 되면 원본 유지 — 너무
    공격적인 stripping 으로 일반 단어가 잘못 매칭되는 것을 막는다. S2 보강:
    3자 토큰 "결제팀" 의 stem 도 후보로 살려야 하므로 2자까지 허용 (이후
    extract 단계에서 길이 3 이상 컷오프로 통제).
    """
    m = _KOREAN_JOSA_RE.search(token)
    if not m:
        return token
    stem = token[: m.start()]
    if len(stem) < min_stem_len:
        return token
    return stem


def extract_korean_proper_noun_candidates(
    text: str,
    *,
    max_freq: int = 1,
) -> set[str]:
    """청크에서 한국어 고유명사 후보(stem) 를 추출한다.

    - 3자 이상의 연속 한글 토큰을 정규식으로 후보화 (S2 보강 — 3자 고유명사
      "결제팀", "주문봇" 등 포함)
    - 후행 조사를 1회 제거해 stem 으로 정규화 ("결제한도서비스는" → "결제한도서비스")
    - 청크 내 stem 빈도 ``max_freq`` 이하 (= 고유명사 가능성 높음)
    - stem 이 ``_KOREAN_COMMON_NOUNS`` 화이트리스트면 제외
    - stem 길이 3자 이상만 유지 (S2 — v2의 4자 컷오프 완화)

    누출 검사용 후보 — 한국어 토큰은 조사가 풍부해 단순 토큰 매칭이 실패하므로
    stem 매칭이 필수다.
    """
    counts: dict[str, int] = {}
    for m in _KOREAN_NOUN_RE.finditer(text):
        tok = m.group(0)
        stem = _strip_korean_josa(tok)
        if len(stem) < 3:
            continue
        if stem in _KOREAN_COMMON_NOUNS:
            continue
        counts[stem] = counts.get(stem, 0) + 1
    return {stem for stem, c in counts.items() if c <= max_freq}


def has_korean_proper_noun_leakage(question: str, source_text: str) -> bool:
    """질문이 출처 청크의 한국어 고유명사를 그대로 베꼈는지 검사.

    한국어 사내 문서(팀명·시스템명·서비스명)는 ASCII 식별자 정규식으로 잡히지 않아
    별도 게이트가 필요하다. 청크 내 빈도가 낮은(고유명사 가능성 높은) 4자 이상 한글
    토큰의 stem 이 질문에 substring 으로 등장하면 누출로 판정한다.

    True 면 골드셋에서 탈락 — 검색 시스템이 어휘 매칭만으로 1위를 차지해 검색 품질
    측정이 부풀려지는 것을 막는다.
    """
    candidates = extract_korean_proper_noun_candidates(source_text)
    if not candidates:
        return False
    for stem in candidates:
        # stem 이 질문에 substring 으로 등장하면 누설 (조사가 달라도 잡기 위함)
        if stem in question:
            return True
    return False


# ---------------------------------------------------------------------------
# Demonstrative reference detection (지시대명사·포인터 표현 탐지)
# ---------------------------------------------------------------------------


_DEMONSTRATIVE_RE = re.compile(
    # 한글: 지시어(이/위/아래/다음/해당/본) + (선택 공백) + 코드/그래프 단위.
    # 분류어 화이트리스트로 "이메일/이벤트/위치/위반/본문/다음과" 같은 합성어
    # false positive 를 자연스럽게 회피한다.
    r"(?:이|위|아래|다음|해당|본)\s*"
    r"(?:클래스|메서드|메소드|함수|코드|모듈|타입|구조체|인터페이스|"
    r"객체|스니펫|예제|예시|구현|로직|엔티티|노드|관계)"
    # 한글: "위/아래에 있는"
    r"|(?:위|아래)에\s*있는"
    # 영어: this + 코드/그래프 단위
    r"|\bthis\s+(?:class|method|function|code|module|type|struct|"
    r"interface|object|snippet|example|implementation|entity|node|"
    r"relation|edge)\b"
    # 영어: the above/below/following/preceding + 코드 단위
    r"|\bthe\s+(?:above|below|following|preceding)\s+"
    r"(?:class|method|function|code|module|snippet|example|entity|"
    r"node|relation|edge)\b",
    flags=re.IGNORECASE,
)


def has_demonstrative_reference(question: str) -> bool:
    """질문이 청크를 가리키는 지시대명사·포인터 표현을 포함하는지 검사.

    True 면 "이 클래스/위 코드/this method" 처럼 단독 검색어로 의미가
    성립하지 않는 질문 — 골드셋에서 탈락시킨다. 검색 시스템은 청크 포인터
    없이 질의만 받으므로 이런 표현은 측정 노이즈일 뿐이다.

    LLM 호출 없는 결정론적 검사. 프롬프트 (``GENERATE_PROMPT_TEMPLATE``) 의
    명시 금지 조항을 슬립스루하는 케이스를 마지막에 차단한다.
    """
    return bool(_DEMONSTRATIVE_RE.search(question))


# ---------------------------------------------------------------------------
# LLM 응답 파싱
# ---------------------------------------------------------------------------


def parse_generated_questions(text: str) -> list[GeneratedQuestion]:
    """Generator LLM 응답에서 질문 리스트를 파싱한다.

    파싱 실패 시 빈 리스트 반환 (호출부에서 게이트 처리).
    """
    try:
        data = extract_json(text)
    except ValueError:
        logger.warning("질문 생성 응답 파싱 실패: %s", text[:200])
        return []
    if not isinstance(data, list):
        return []
    out: list[GeneratedQuestion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or item.get("query") or "").strip()
        if not q:
            continue
        diff = str(item.get("difficulty") or "").strip().lower()
        if diff not in ("easy", "medium", "hard"):
            diff = ""
        out.append(GeneratedQuestion(query=q, difficulty=diff))
    return out


def parse_generated_graph_questions(text: str) -> list[GeneratedGraphQuestion]:
    """그래프 모드 Generator 응답 파싱 (2차 — evidence/aliases/relation 포함).

    1차 호환: ``evidence_description`` / ``entity_aliases`` / ``relation`` 이
    누락된 응답이면 기본값(빈 문자열·빈 리스트·None) 으로 채워 반환한다.
    질문 본문(``q`` 또는 ``query``) 만 있어도 정상 파싱된다.
    """
    try:
        data = extract_json(text)
    except ValueError:
        logger.warning("graph 질문 응답 파싱 실패: %s", text[:200])
        return []
    if not isinstance(data, list):
        return []
    out: list[GeneratedGraphQuestion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or item.get("query") or "").strip()
        if not q:
            continue
        diff = str(item.get("difficulty") or "").strip().lower()
        if diff not in ("easy", "medium", "hard"):
            diff = ""
        evidence = str(item.get("evidence_description") or "").strip()
        raw_aliases = item.get("entity_aliases") or []
        aliases: list[str] = []
        if isinstance(raw_aliases, list):
            for a in raw_aliases:
                if isinstance(a, str):
                    a_stripped = a.strip()
                    if a_stripped:
                        aliases.append(a_stripped)
        raw_rel = item.get("relation")
        relation: GeneratedGraphRelation | None = None
        if isinstance(raw_rel, dict):
            src = str(raw_rel.get("source_name") or "").strip()
            tgt = str(raw_rel.get("target_name") or "").strip()
            rtype = str(raw_rel.get("relation_type") or "").strip()
            rdesc = str(raw_rel.get("relation_description") or "").strip()
            if src and tgt and rtype:
                relation = GeneratedGraphRelation(
                    source_name=src,
                    target_name=tgt,
                    relation_type=rtype,
                    description=rdesc,
                )
        out.append(GeneratedGraphQuestion(
            query=q,
            difficulty=diff,
            evidence_description=evidence,
            entity_aliases=aliases,
            relation=relation,
        ))
    return out


def parse_yes_no(text: str) -> bool | None:
    """"yes"/"no" 한 단어 응답을 bool 로 파싱.

    Qwen3 의 <think> 태그도 처리. 모호하면 None.
    """
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip().lower()
    # 첫 단어/줄만 본다
    head = text.splitlines()[0] if text else ""
    head = head.strip().strip(".,!?\"'`")
    if head in ("yes", "y", "true", "예", "네"):
        return True
    if head in ("no", "n", "false", "아니오", "아니요"):
        return False
    # 한 단어가 아니어도 첫 토큰이 명확하면 인정
    if head.startswith("yes"):
        return True
    if head.startswith("no"):
        return False
    return None


# ---------------------------------------------------------------------------
# Generation + Filter pipeline
# ---------------------------------------------------------------------------


async def generate_questions(
    chunk_content: str,
    *,
    n: int,
    generator: LLMClient,
    reasoning_mode: str | None = "off",
    max_tokens: int = 1024,
    temperature: float = 0.0,
    seed: int | None = None,
) -> list[GeneratedQuestion]:
    """Generator LLM 에 청크를 보여주고 질문 N개를 생성 요청한다.

    Args:
        temperature: 샘플링 온도. 기본 0.0 (결정적). 다양성 폭 확대가 필요하면
            CLI 옵션으로 0.7 같은 값을 지정 — 단 재현성 손실 트레이드오프.
        seed: 결정성용 seed. endpoint 가 지원하면 전달 (vLLM/OpenAI 호환). 같은
            seed + temperature + 입력 → 같은 응답 (best-effort, model fingerprint
            보장 아님).
    """
    prompt = GENERATE_PROMPT_TEMPLATE.format(chunk_content=chunk_content, n=n)
    call_kwargs: dict[str, Any] = {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_mode": reasoning_mode,
        "purpose": "goldset_generate",
    }
    if seed is not None:
        call_kwargs["seed"] = int(seed)
    text = await generator.complete(prompt, **call_kwargs)
    return parse_generated_questions(text)


async def generate_graph_questions(
    subgraph: dict[str, Any],
    *,
    n: int,
    generator: LLMClient,
    reasoning_mode: str | None = "off",
    max_tokens: int = 1024,
    temperature: float = 0.0,
    seed: int | None = None,
) -> list[GeneratedGraphQuestion]:
    """그래프 subgraph 에서 질문 N개 생성.

    2차 변경: 반환 타입이 :class:`GeneratedGraphQuestion` 으로 확장되어
    ``evidence_description`` / ``entity_aliases`` / ``relation`` 까지 함께
    파싱된다. 1차 호환 — LLM 이 ``q`` 만 반환해도 다른 필드는 기본값.

    Args:
        subgraph: ``load_candidate_subgraphs`` 산출물 dict. ``entity_name`` /
            ``entity_type`` / ``entity_description`` / ``edges`` 키를 사용.
        n: 생성할 질문 수.
        generator: Generator LLM.
        reasoning_mode: 추론 프로파일 (chunk 모드와 동일).
        max_tokens: 응답 최대 토큰.
        temperature: 샘플링 온도 (기본 0.0, 재현성 우선).
        seed: 결정성용 seed (endpoint 지원 시 전달).
    """
    prompt = GRAPH_GENERATE_PROMPT_TEMPLATE.format(
        entity_name=subgraph.get("entity_name", ""),
        entity_type=subgraph.get("entity_type", ""),
        entity_description=subgraph.get("entity_description", "") or "(설명 없음)",
        edges_text=format_edges_for_prompt(subgraph.get("edges") or []),
        n=n,
    )
    call_kwargs: dict[str, Any] = {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_mode": reasoning_mode,
        "purpose": "goldset_generate_graph",
    }
    if seed is not None:
        call_kwargs["seed"] = int(seed)
    text = await generator.complete(prompt, **call_kwargs)
    return parse_generated_graph_questions(text)


async def is_answerable(
    question: str,
    chunk_content: str,
    *,
    judge: LLMClient,
    reasoning_mode: str | None = "off",
) -> bool | None:
    """Judge LLM 에 "이 청크로 이 질문 답할 수 있는가?" 를 물어 yes/no 로 받는다.

    파싱 실패 시 None.
    """
    prompt = ANSWERABLE_PROMPT_TEMPLATE.format(
        question=question, chunk_content=chunk_content,
    )
    text = await judge.complete(
        prompt,
        max_tokens=64,
        temperature=0.0,
        reasoning_mode=reasoning_mode,
        purpose="goldset_judge_answerable",
    )
    return parse_yes_no(text)


async def is_unique_source(
    question: str,
    chunk_content: str,
    *,
    judge: LLMClient,
    reasoning_mode: str | None = "off",
) -> bool | None:
    """Judge LLM 에 "이 청크가 이 질문의 유일한 정답 출처인가?" 를 물어 yes/no.

    ``is_answerable`` 과 의미가 다른 별도 게이트 — 답변 가능성은 동일 프롬프트로
    중복 검증되지 않는다. 일반적인 위키·매뉴얼·외부 지식으로 답할 수 있는 질문은
    no 로 분류되어, 검색 시스템 평가에서 trivial recall 을 유발하는 generic 질문을
    걸러낸다.

    파싱 실패 시 None.
    """
    prompt = GENERIC_PROMPT_TEMPLATE.format(
        question=question, chunk_content=chunk_content,
    )
    text = await judge.complete(
        prompt,
        max_tokens=64,
        temperature=0.0,
        reasoning_mode=reasoning_mode,
        purpose="goldset_judge_unique_source",
    )
    return parse_yes_no(text)


async def filter_question(
    question: str,
    source_chunk: str,
    distractors: list[str],
    *,
    judge: LLMClient,
    reasoning_mode: str | None = "off",
) -> FilterReport:
    """다단계 품질 게이트.

    적용 순서 (먼저 결정론적 게이트, 그다음 LLM 게이트):
        (a) 답변 가능성 — 정답 청크로 답할 수 있는가? (LLM, ``is_answerable``)
        (b1) ASCII 식별자 누출 — 결정론
        (b2) 한국어 고유명사 누출 — 결정론
        (c) 지시대명사·포인터 표현 — 결정론
        (d1) 정답 청크 유일성 — 정답 청크가 유일한 출처인가? (LLM, ``is_unique_source``,
             ``is_answerable`` 과 의미가 명확히 분리된 프롬프트)
        (d2) Distractor 보조 검증 — 무관 청크로도 답할 수 있으면 generic

    탈락 사유 (FilterReport.reason):
        ``not_answerable`` | ``leakage`` | ``korean_leakage`` |
        ``demonstrative`` | ``non_unique_source`` | ``generic`` | ``parse_error``

    Args:
        question: 후보 질문.
        source_chunk: 출처 청크 (정답 청크).
        distractors: 무관 청크 N개 (일반성 보조 검증용).
        judge: Judge LLM.
    """
    # (a) 답변 가능성 — 출처 청크로 답해야 함
    ans = await is_answerable(question, source_chunk, judge=judge, reasoning_mode=reasoning_mode)
    if ans is None:
        return FilterReport(passed=False, reason="parse_error")
    if not ans:
        return FilterReport(passed=False, reason="not_answerable")

    # (b1) ASCII 식별자 누출 — 결정론적 (LLM 호출 없음)
    if has_identifier_leakage(question, source_chunk):
        return FilterReport(passed=False, reason="leakage")

    # (b2) 한국어 고유명사 누출 — 결정론적. 사내 한국어 문서의 팀명·시스템명
    # 같은 고유명사가 질문에 그대로 베껴진 케이스를 잡는다.
    if has_korean_proper_noun_leakage(question, source_chunk):
        return FilterReport(passed=False, reason="korean_leakage")

    # (c) 지시대명사·포인터 표현 — 결정론적 (LLM 호출 없음).
    # "이 클래스/위 코드/this method" 처럼 청크 포인터를 가정하는 질문은
    # 검색 시스템에는 의미가 없으므로 탈락.
    if has_demonstrative_reference(question):
        return FilterReport(passed=False, reason="demonstrative")

    # (d1) 유일성 — 정답 청크가 유일한 출처인지 확인. ``is_answerable`` 과
    # 의미가 명확히 분리된 프롬프트(GENERIC_PROMPT_TEMPLATE)로 호출하여,
    # 동일 LLM 호출이 두 게이트를 동시에 통과시키는 self-bias 를 차단한다.
    unique = await is_unique_source(
        question, source_chunk, judge=judge, reasoning_mode=reasoning_mode,
    )
    if unique is None:
        return FilterReport(passed=False, reason="parse_error")
    if not unique:
        return FilterReport(passed=False, reason="non_unique_source")

    # (d2) Distractor 보조 검증 — 무관 청크로도 답할 수 있으면 정답 청크 유일성이 깨짐
    for distractor in distractors:
        ans = await is_answerable(question, distractor, judge=judge, reasoning_mode=reasoning_mode)
        if ans is True:
            return FilterReport(passed=False, reason="generic")

    return FilterReport(passed=True)


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------


def stratified_sample(
    candidates: list[dict[str, Any]],
    *,
    n_total: int,
    key: str = "source_type",
    rng: Any = None,
) -> list[dict[str, Any]]:
    """``key`` 별로 균등 분포가 되도록 샘플링한다.

    각 그룹에서 round-robin 으로 뽑아 한쪽에 쏠리지 않게 한다.

    Args:
        candidates: 후보 dict 리스트.
        n_total: 총 샘플 수.
        key: 그룹 기준 키.
        rng: 셔플용 random.Random 인스턴스. None 이면 결정론적 (정렬 순서 유지).
    """
    if n_total <= 0 or not candidates:
        return []

    groups: dict[Any, list[dict[str, Any]]] = {}
    for c in candidates:
        groups.setdefault(c.get(key, "_unknown"), []).append(c)

    if rng is not None:
        for g in groups.values():
            rng.shuffle(g)

    selected: list[dict[str, Any]] = []
    group_iters = {k: iter(v) for k, v in groups.items()}
    exhausted: set[Any] = set()

    while len(selected) < n_total and len(exhausted) < len(groups):
        for k in list(group_iters.keys()):
            if k in exhausted:
                continue
            try:
                selected.append(next(group_iters[k]))
                if len(selected) >= n_total:
                    break
            except StopIteration:
                exhausted.add(k)

    return selected
