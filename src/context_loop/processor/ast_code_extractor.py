"""AST 기반 코드 심볼 추출 모듈.

코드 파일에서 함수/클래스/타입 정의를 심볼 단위로 분할하고,
import 관계를 추출한다. LLM 호출 없이 순수 정적 분석으로 동작한다.

지원 언어:
- Python: ast 모듈 기반 정확한 파싱
- Go, Java, TypeScript, JavaScript: 키워드 + 중괄호 매칭
- 기타: 파일 전체를 단일 심볼로 반환
"""

from __future__ import annotations

import ast
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from context_loop.processor.chunker import Chunk, count_tokens
from context_loop.processor.graph_extractor import Entity, GraphData, Relation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CodeSymbol:
    """코드 심볼 (함수, 클래스, 타입 등).

    Attributes:
        name: 심볼 이름.
        symbol_type: 심볼 유형 (function, class, struct, interface).
        signature: 함수/타입 시그니처.
        body: 심볼 전체 소스 코드.
        line_start: 시작 라인 (1-based).
        line_end: 종료 라인 (1-based).
        docstring: 독스트링 또는 주석.
    """

    name: str
    symbol_type: str
    signature: str
    body: str
    line_start: int
    line_end: int
    docstring: str = ""


@dataclass
class CodeExtraction:
    """코드 추출 결과.

    Attributes:
        file_path: 파일 경로.
        language: 프로그래밍 언어.
        symbols: 추출된 심볼 목록.
        imports: import된 모듈 이름 목록.
    """

    file_path: str
    language: str
    symbols: list[CodeSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
}

_BRACE_LANGUAGES = frozenset({"go", "java", "typescript", "javascript"})

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_code_symbols(content: str, file_path: str) -> CodeExtraction:
    """코드 파일에서 심볼과 import를 추출한다.

    Args:
        content: 소스 코드 문자열.
        file_path: 파일 경로 (언어 감지용).

    Returns:
        CodeExtraction 결과.
    """
    ext = Path(file_path).suffix.lower()
    language = _LANG_MAP.get(ext, "unknown")

    if not content.strip():
        return CodeExtraction(file_path=file_path, language=language)

    try:
        if language == "python":
            symbols, imports = _extract_python(content)
        elif language in _BRACE_LANGUAGES:
            symbols, imports = _extract_brace_language(content, language)
        else:
            symbols, imports = _extract_fallback(content, file_path)
    except Exception:
        logger.warning(
            "코드 심볼 추출 실패: %s, 전체 파일로 대체", file_path, exc_info=True,
        )
        symbols, imports = _extract_fallback(content, file_path)

    return CodeExtraction(
        file_path=file_path,
        language=language,
        symbols=symbols,
        imports=imports,
    )


def to_chunks(
    extraction: CodeExtraction,
    file_title: str,
) -> tuple[list[Chunk], list[str]]:
    """CodeExtraction을 Chunk 리스트 + 임베딩 텍스트로 변환한다.

    각 심볼(함수/클래스)이 하나의 청크가 된다.

    Returns:
        (chunks, embed_texts) 튜플.
        - chunks: 전체 코드가 포함된 Chunk 리스트 (검색 결과로 반환될 내용).
        - embed_texts: 검색용 임베딩 텍스트 리스트 (이름 + 시그니처 + docstring).
          전체 코드 대신 의미 요약만 임베딩하여 자연어 질의와의 유사도를 높인다.
    """
    chunks: list[Chunk] = []
    embed_texts: list[str] = []

    for i, sym in enumerate(extraction.symbols):
        # 저장/반환용: 전체 코드
        header = f"# File: {file_title}\n# {sym.symbol_type}: {sym.signature}\n\n"
        chunk_content = header + sym.body

        chunks.append(Chunk(
            id=str(uuid.uuid4()),
            index=i,
            content=chunk_content,
            token_count=count_tokens(chunk_content),
            section_path=f"{file_title} > {sym.name}",
        ))

        # 임베딩용: 이름 + 시그니처 + docstring
        embed_parts = [file_title, sym.name, sym.signature]
        if sym.docstring:
            embed_parts.append(sym.docstring)
        embed_texts.append("\n".join(embed_parts))

    return chunks, embed_texts


def to_graph_data(extraction: CodeExtraction, file_title: str) -> GraphData:
    """CodeExtraction을 GraphData로 변환한다.

    - 엔티티: 파일(module) + 각 심볼 (함수/클래스)
    - 관계: import 관계만 (파일 → imports → 모듈)
    """
    entities: list[Entity] = [
        Entity(
            name=file_title,
            entity_type="module",
            description=f"{extraction.language} file, {len(extraction.symbols)} symbols",
        ),
    ]

    for sym in extraction.symbols:
        entities.append(Entity(
            name=sym.name,
            entity_type=sym.symbol_type,
            description=sym.signature,
        ))

    relations: list[Relation] = [
        Relation(
            source=file_title,
            target=imp,
            relation_type="imports",
        )
        for imp in extraction.imports
    ]

    return GraphData(entities=entities, relations=relations)


# ---------------------------------------------------------------------------
# Python extractor (ast module)
# ---------------------------------------------------------------------------


def _extract_python(content: str) -> tuple[list[CodeSymbol], list[str]]:
    """Python AST를 사용한 심볼 추출."""
    tree = ast.parse(content)
    lines = content.splitlines(keepends=True)

    symbols: list[CodeSymbol] = []
    imports: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = "".join(lines[node.lineno - 1 : node.end_lineno])
            sig = _python_func_sig(node)
            symbols.append(CodeSymbol(
                name=node.name,
                symbol_type="function",
                signature=sig,
                body=body,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=ast.get_docstring(node) or "",
            ))
        elif isinstance(node, ast.ClassDef):
            body = "".join(lines[node.lineno - 1 : node.end_lineno])
            symbols.append(CodeSymbol(
                name=node.name,
                symbol_type="class",
                signature=f"class {node.name}",
                body=body,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=ast.get_docstring(node) or "",
            ))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return symbols, imports


def _python_func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Python 함수 시그니처 문자열을 생성한다."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    parts: list[str] = []
    for arg in node.args.args:
        name = arg.arg
        if arg.annotation:
            try:
                name += f": {ast.unparse(arg.annotation)}"
            except Exception:
                pass
        parts.append(name)

    ret = ""
    if node.returns:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:
            pass

    return f"{prefix} {node.name}({', '.join(parts)}){ret}"


# ---------------------------------------------------------------------------
# Brace-language extractor (Go, Java, TypeScript, JavaScript)
# ---------------------------------------------------------------------------

# 함수/타입 정의를 감지하는 패턴 (언어별)
_DEFINITION_PATTERNS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "go": [
        (re.compile(r"^func\s+(?:\([^)]*\)\s+)?(\w+)\s*\("), "function"),
        (re.compile(r"^type\s+(\w+)\s+struct\b"), "struct"),
        (re.compile(r"^type\s+(\w+)\s+interface\b"), "interface"),
    ],
    "java": [
        (re.compile(
            r"^\s*(?:public|private|protected|static|final|abstract|\s)*"
            r"(?:class|interface|enum)\s+(\w+)",
        ), "class"),
        (re.compile(
            r"^\s*(?:public|private|protected|static|final|synchronized|\s)*"
            r"[\w<>\[\],\s]+\s+(\w+)\s*\(",
        ), "function"),
    ],
    "typescript": [
        (re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
        (re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
        (re.compile(r"^(?:export\s+)?interface\s+(\w+)"), "interface"),
    ],
    "javascript": [
        (re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
        (re.compile(r"^(?:export\s+)?class\s+(\w+)"), "class"),
    ],
}

# import 추출 패턴 (언어별)
_IMPORT_PATTERNS: dict[str, re.Pattern[str]] = {
    "go": re.compile(r'"([^"]+)"'),
    "java": re.compile(r"^\s*import\s+([\w.]+)\s*;", re.MULTILINE),
    "typescript": re.compile(r"""from\s+['"]([^'"]+)['"]"""),
    "javascript": re.compile(
        r"""(?:from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    ),
}


def _extract_brace_language(
    content: str,
    language: str,
) -> tuple[list[CodeSymbol], list[str]]:
    """중괄호 기반 언어에서 심볼과 import를 추출한다."""
    symbols = _extract_brace_symbols(content, language)
    imports = _extract_brace_imports(content, language)
    return symbols, imports


def _extract_brace_symbols(content: str, language: str) -> list[CodeSymbol]:
    """키워드 감지 + 중괄호 매칭으로 심볼을 추출한다."""
    patterns = _DEFINITION_PATTERNS.get(language, [])
    if not patterns:
        return []

    lines = content.splitlines(keepends=True)
    symbols: list[CodeSymbol] = []
    used_ranges: list[tuple[int, int]] = []

    for line_idx, line in enumerate(lines):
        # 이미 다른 심볼 범위에 포함된 라인은 건너뜀
        if any(s <= line_idx < e for s, e in used_ranges):
            continue

        for pattern, sym_type in patterns:
            m = pattern.match(line.strip())
            if not m:
                continue

            name = m.group(1)
            # 해당 라인부터의 오프셋으로 블록 끝 찾기
            block_start = sum(len(l) for l in lines[:line_idx])
            brace_pos = content.find("{", block_start)
            if brace_pos == -1:
                continue

            block_end = _find_matching_brace(content, brace_pos)
            body = content[block_start:block_end]
            end_line = content[:block_end].count("\n") + 1

            # 시그니처: 첫 번째 줄 (또는 { 이전까지)
            sig_end = body.find("{")
            signature = body[:sig_end].strip() if sig_end != -1 else line.strip()

            # docstring: 직전 줄의 주석
            docstring = _extract_preceding_comment(lines, line_idx)

            symbols.append(CodeSymbol(
                name=name,
                symbol_type=sym_type,
                signature=signature,
                body=body,
                line_start=line_idx + 1,
                line_end=end_line,
                docstring=docstring,
            ))
            used_ranges.append((line_idx, end_line))
            break  # 한 라인에서 하나의 패턴만 매칭

    return symbols


def _extract_brace_imports(content: str, language: str) -> list[str]:
    """import 패턴으로 모듈 이름을 추출한다."""
    pattern = _IMPORT_PATTERNS.get(language)
    if pattern is None:
        return []

    imports: list[str] = []
    for m in pattern.finditer(content):
        # 여러 그룹 중 매칭된 것 사용 (JavaScript require/from 패턴)
        for g in m.groups():
            if g:
                imports.append(g)
                break
    return list(dict.fromkeys(imports))  # 순서 유지 중복 제거


def _find_matching_brace(content: str, open_pos: int) -> int:
    """open_pos의 { 에 대응하는 } 위치 다음 인덱스를 반환한다."""
    depth = 0
    in_string = False
    string_char = ""
    i = open_pos

    while i < len(content):
        ch = content[i]

        if in_string:
            if ch == "\\" and i + 1 < len(content):
                i += 2
                continue
            if ch == string_char:
                in_string = False
        else:
            if ch in ('"', "'", "`"):
                in_string = True
                string_char = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1

    return len(content)


def _extract_preceding_comment(lines: list[str], line_idx: int) -> str:
    """심볼 직전의 연속 주석 블록을 추출한다."""
    comments: list[str] = []
    idx = line_idx - 1
    while idx >= 0:
        stripped = lines[idx].strip()
        if stripped.startswith("//") or stripped.startswith("#"):
            comments.append(stripped.lstrip("/#").strip())
            idx -= 1
        elif stripped.startswith("*") or stripped.startswith("/*"):
            comments.append(stripped.lstrip("/*").rstrip("*/").strip())
            idx -= 1
        else:
            break
    comments.reverse()
    return "\n".join(comments) if comments else ""


# ---------------------------------------------------------------------------
# Fallback: 파일 전체를 단일 심볼로
# ---------------------------------------------------------------------------


def _extract_fallback(
    content: str,
    file_path: str,
) -> tuple[list[CodeSymbol], list[str]]:
    """파싱 불가능한 파일은 전체를 단일 심볼로 반환한다."""
    name = Path(file_path).stem
    return (
        [CodeSymbol(
            name=name,
            symbol_type="module",
            signature=Path(file_path).name,
            body=content,
            line_start=1,
            line_end=content.count("\n") + 1,
        )],
        [],
    )
