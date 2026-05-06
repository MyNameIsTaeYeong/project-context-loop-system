"""LLM 기반 골드셋 합성 — 프롬프트 빌더와 품질 게이트.

원리:
1. 청크를 보여주고 Generator LLM 에 N개 질문 생성 요청 (역방향 생성).
2. Judge LLM 으로 3 단계 품질 게이트 적용:
   (a) 답변 가능성 — 출처 청크만 보고 답할 수 있는가?
   (b) 식별자 누출 — 청크 고유 식별자가 질문에 그대로 들어갔는가?
   (c) 일반성 — 무관한 다른 청크로도 답할 수 있는가?
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
from pathlib import Path
from typing import Any

from context_loop.config import Config
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
class FilterReport:
    """품질 게이트 통과/탈락 사유 리포트."""

    passed: bool
    reason: str = ""  # 탈락 시 원인 (answerable | leakage | generic | parse_error)


# ---------------------------------------------------------------------------
# Prompts (모듈 변수로 노출하여 테스트/튜닝 가능)
# ---------------------------------------------------------------------------


GENERATE_PROMPT_TEMPLATE = """\
다음은 사내 문서 또는 코드의 한 청크다:

---
{chunk_content}
---

이 청크가 정답이 되도록, 사람이 자연스럽게 물어볼 만한 한국어 질문을 {n}개 생성해라.

조건:
- 한국어 자연어 질문 (의문문)
- 식별자(함수명/클래스명/변수명/페이지명)를 그대로 베끼지 말고 의미 단위로 풀어쓸 것
  ✗ 나쁜 예: "_clamp_max_per_tenant 함수가 뭔가요?"
  ○ 좋은 예: "테넌트별 최대치 제한 로직은 어떻게 동작하나요?"
- 청크 안의 정보로 답할 수 있는 질문일 것 (외부 지식 요구 금지)
- 난이도를 골고루 분포: easy(직접 사실 조회) / medium(개념 이해) / hard(원인·관계·왜)

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

이 문맥만 보고 위 질문에 사실 기반으로 답할 수 있는가?
yes/no 한 단어로만 답하라.
"""


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
) -> list[GeneratedQuestion]:
    """Generator LLM 에 청크를 보여주고 질문 N개를 생성 요청한다."""
    prompt = GENERATE_PROMPT_TEMPLATE.format(chunk_content=chunk_content, n=n)
    text = await generator.complete(
        prompt,
        max_tokens=max_tokens,
        temperature=0.7,
        reasoning_mode=reasoning_mode,
        purpose="goldset_generate",
    )
    return parse_generated_questions(text)


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


async def filter_question(
    question: str,
    source_chunk: str,
    distractors: list[str],
    *,
    judge: LLMClient,
    reasoning_mode: str | None = "off",
) -> FilterReport:
    """3 단계 품질 게이트.

    Args:
        question: 후보 질문.
        source_chunk: 출처 청크 (정답 청크).
        distractors: 무관 청크 N개 (일반성 검증용).
        judge: Judge LLM.
    """
    # (a) 답변 가능성 — 출처 청크로 답해야 함
    ans = await is_answerable(question, source_chunk, judge=judge, reasoning_mode=reasoning_mode)
    if ans is None:
        return FilterReport(passed=False, reason="parse_error")
    if not ans:
        return FilterReport(passed=False, reason="not_answerable")

    # (b) 식별자 누출 — 결정론적 (LLM 호출 없음)
    if has_identifier_leakage(question, source_chunk):
        return FilterReport(passed=False, reason="leakage")

    # (c) 일반성 — 무관 청크로도 답할 수 있으면 정답 청크 유일성이 깨짐
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


# ---------------------------------------------------------------------------
# 합성 실행 파라미터 — config.eval.synth.* + CLI override 합성
# ---------------------------------------------------------------------------


@dataclass
class SynthRunConfig:
    """build_synthetic_gold_set.py 의 실행 파라미터.

    각 필드의 기본값은 ``config.eval.synth.*`` 에서 로드되며 CLI 인자로
    덮어쓸 수 있다. ``resolve_synth_run_config`` 가 합성 책임을 진다.
    """

    output_path: Path
    n_chunks: int
    questions_per_chunk: int
    source_types: list[str] | None
    n_distractors: int
    min_chars: int
    max_chars: int
    reasoning_mode: str
    seed: int | None
    apply_filter: bool
    metadata: dict[str, Any] = field(default_factory=dict)


def resolve_synth_run_config(
    config: Config,
    *,
    output: str | None = None,
    n_chunks: int | None = None,
    questions_per_chunk: int | None = None,
    source_types: str | None = None,
    n_distractors: int | None = None,
    min_chars: int | None = None,
    max_chars: int | None = None,
    reasoning_mode: str | None = None,
    seed: int | None = None,
    no_filter: bool = False,
) -> SynthRunConfig:
    """CLI 인자(``None`` = 미지정) + ``config.eval.synth.*`` 를 합성한다.

    우선순위 (높음 → 낮음):
        1. CLI 인자 (호출 시 ``None`` 이 아닌 값)
        2. ``config.eval.synth.{key}``
        3. 마지막 폴백 (default.yaml 의 값과 동일하게 유지)

    ``no_filter`` 만 예외적으로 CLI 플래그(action="store_true")이므로 True 면
    무조건 게이트 OFF (디버그 전용 — config 무관).

    ``source_types`` CLI 인자는 콤마 분리 문자열, config 는 list. 빈 값은
    ``None`` (= 전체 소스 사용).

    Args:
        config: 애플리케이션 Config.
        output, n_chunks, ...: CLI 인자 값. ``None`` 이면 config 사용.
        no_filter: True 면 품질 게이트 강제 OFF.

    Returns:
        ``SynthRunConfig`` — 그대로 ``build()`` 에 넘길 수 있다.
    """
    cfg_output = config.get("eval.synth.output", "eval/gold_set.yaml")
    cfg_n_chunks = config.get("eval.synth.n_chunks", 30)
    cfg_qpc = config.get("eval.synth.questions_per_chunk", 2)
    cfg_source_types = config.get("eval.synth.source_types") or []
    cfg_n_distractors = config.get("eval.synth.n_distractors", 2)
    cfg_min_chars = config.get("eval.synth.min_chars", 200)
    cfg_max_chars = config.get("eval.synth.max_chars", 8000)
    cfg_reasoning = config.get("eval.synth.reasoning_mode", "off")
    cfg_seed = config.get("eval.synth.seed")  # null 가능

    if source_types is not None and source_types != "":
        # CLI: 콤마 분리 문자열 → list (빈 값 제거)
        resolved_source_types: list[str] | None = [
            s.strip() for s in source_types.split(",") if s.strip()
        ] or None
    elif cfg_source_types:
        if not isinstance(cfg_source_types, list):
            raise ValueError(
                f"config.eval.synth.source_types 는 list 이어야 합니다 — "
                f"got {type(cfg_source_types).__name__}",
            )
        resolved_source_types = [str(s) for s in cfg_source_types] or None
    else:
        resolved_source_types = None

    return SynthRunConfig(
        output_path=Path(output if output else cfg_output),
        n_chunks=int(n_chunks if n_chunks is not None else cfg_n_chunks),
        questions_per_chunk=int(
            questions_per_chunk if questions_per_chunk is not None else cfg_qpc,
        ),
        source_types=resolved_source_types,
        n_distractors=int(
            n_distractors if n_distractors is not None else cfg_n_distractors,
        ),
        min_chars=int(min_chars if min_chars is not None else cfg_min_chars),
        max_chars=int(max_chars if max_chars is not None else cfg_max_chars),
        reasoning_mode=str(
            reasoning_mode if reasoning_mode is not None else cfg_reasoning,
        ),
        seed=seed if seed is not None else (int(cfg_seed) if cfg_seed is not None else None),
        apply_filter=not no_filter,
    )
