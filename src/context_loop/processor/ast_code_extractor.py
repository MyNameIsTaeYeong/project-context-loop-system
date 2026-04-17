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
    """코드 심볼 (함수, 클래스, 메서드, 타입 등).

    Attributes:
        name: 심볼 이름.
        symbol_type: 심볼 유형 (function, class, method, struct, interface).
        signature: 함수/타입 시그니처.
        body: 심볼 전체 소스 코드.
        line_start: 시작 라인 (1-based).
        line_end: 종료 라인 (1-based).
        docstring: 독스트링 또는 주석.
        parent_name: 소속 클래스/구조체 이름. 최상위 심볼은 빈 문자열.
        parent_signature: 소속 클래스의 시그니처. 최상위 심볼은 빈 문자열.
    """

    name: str
    symbol_type: str
    signature: str
    body: str
    line_start: int
    line_end: int
    docstring: str = ""
    parent_name: str = ""
    parent_signature: str = ""


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
        if sym.parent_name:
            header = (
                f"# File: {file_title}\n"
                f"# {sym.parent_signature}\n"
                f"# {sym.symbol_type}: {sym.signature}\n\n"
            )
            section_path = f"{file_title} > {sym.parent_name} > {sym.name}"
        else:
            header = f"# File: {file_title}\n# {sym.symbol_type}: {sym.signature}\n\n"
            section_path = f"{file_title} > {sym.name}"

        chunk_content = header + sym.body

        chunks.append(Chunk(
            id=str(uuid.uuid4()),
            index=i,
            content=chunk_content,
            token_count=count_tokens(chunk_content),
            section_path=section_path,
        ))

        # 임베딩용: 이름 + 시그니처 + docstring + (부모 클래스)
        embed_parts = [file_title]
        if sym.parent_name:
            embed_parts.append(sym.parent_name)
        embed_parts.extend([sym.name, sym.signature])
        if sym.docstring:
            embed_parts.append(sym.docstring)
        embed_texts.append("\n".join(embed_parts))

    return chunks, embed_texts


def _symbol_fqn(file_title: str, parent_name: str, name: str) -> str:
    """코드 심볼의 파일 범위 정규화 이름(FQN)을 생성한다.

    예: "user_service.py::UserService.__init__", "reader.py::main".
    """
    if parent_name:
        return f"{file_title}::{parent_name}.{name}"
    return f"{file_title}::{name}"


def _class_fqn(file_title: str, name: str) -> str:
    """클래스/구조체의 파일 범위 정규화 이름(FQN)을 생성한다."""
    return f"{file_title}::{name}"


def to_graph_data(extraction: CodeExtraction, file_title: str) -> GraphData:
    """CodeExtraction을 GraphData로 변환한다.

    - 엔티티: 파일(module) + 각 심볼 (함수/클래스) + import된 모듈
    - 관계: import 관계 (파일 → imports → 모듈), contains (클래스 → 메서드)

    엔티티 이름 규칙:
    - 파일 엔티티: file_title (단순 이름) — 전역 고유
    - import 모듈: 단순 이름 — 파일 간 canonical 병합 의도
      (예: 여러 파일이 `logging`을 import하면 하나의 정규 노드로 병합)
    - 코드 심볼(함수/메서드/클래스/구조체/인터페이스): 파일 범위 FQN
      (예: `user_service.py::UserService.__init__`) — 서로 다른 파일/클래스의
      동명 심볼이 graph_store의 canonical 병합으로 잘못 합쳐지는 것을 방지.
    """
    entities: list[Entity] = [
        Entity(
            name=file_title,
            entity_type="module",
            description=f"{extraction.language} file, {len(extraction.symbols)} symbols",
        ),
    ]

    # import된 모듈을 엔티티로 등록 (관계 target resolve용)
    # file_title과 이름이 겹치는 경우는 스킵하여 자기 자신 엔티티를 덮지 않는다.
    seen_imports: set[str] = set()
    for imp in extraction.imports:
        if imp == file_title or imp in seen_imports:
            continue
        seen_imports.add(imp)
        entities.append(Entity(
            name=imp,
            entity_type="module",
            description="",
        ))

    # 코드 심볼 엔티티 (FQN) — 실제 symbol_type을 우선 보존하기 위해
    # parent_names 루프보다 먼저 수행한다. Go 구조체처럼 top-level 심볼로
    # 존재하면서 동시에 메서드의 parent인 경우, "struct" 타입으로 먼저 등록되어
    # 이후 parent_names 루프에서 "class" 타입으로 중복 등록되지 않는다.
    added_fqns: set[str] = set()
    for sym in extraction.symbols:
        fqn = _symbol_fqn(file_title, sym.parent_name, sym.name)
        if fqn in added_fqns:
            continue
        added_fqns.add(fqn)
        entities.append(Entity(
            name=fqn,
            entity_type=sym.symbol_type,
            description=sym.signature,
        ))

    # 부모 클래스 엔티티 (FQN) — 이미 top-level 심볼로 등록된 경우는 스킵
    for sym in extraction.symbols:
        if not sym.parent_name:
            continue
        parent_fqn = _class_fqn(file_title, sym.parent_name)
        if parent_fqn in added_fqns:
            continue
        added_fqns.add(parent_fqn)
        entities.append(Entity(
            name=parent_fqn,
            entity_type="class",
            description=sym.parent_signature or f"class {sym.parent_name}",
        ))

    relations: list[Relation] = [
        Relation(
            source=file_title,
            target=imp,
            relation_type="imports",
        )
        for imp in extraction.imports
    ]

    # 메서드 → 클래스 contains 관계 (FQN 사용)
    for sym in extraction.symbols:
        if sym.parent_name:
            relations.append(Relation(
                source=_class_fqn(file_title, sym.parent_name),
                target=_symbol_fqn(file_title, sym.parent_name, sym.name),
                relation_type="contains",
            ))

    return GraphData(entities=entities, relations=relations)


# ---------------------------------------------------------------------------
# Python extractor (ast module)
# ---------------------------------------------------------------------------


def _extract_python(content: str) -> tuple[list[CodeSymbol], list[str]]:
    """Python AST를 사용한 심볼 추출.

    클래스는 메서드 단위로 분할하여 각 메서드를 개별 심볼로 추출한다.
    메서드가 없는 클래스는 클래스 전체를 단일 심볼로 추출한다.
    """
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
            class_sig = f"class {node.name}"
            class_doc = ast.get_docstring(node) or ""

            # 클래스 내부 메서드를 개별 심볼로 추출
            methods = [
                child for child in ast.iter_child_nodes(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]

            if methods:
                for method in methods:
                    method_body = "".join(
                        lines[method.lineno - 1 : method.end_lineno],
                    )
                    method_sig = _python_func_sig(method)
                    symbols.append(CodeSymbol(
                        name=method.name,
                        symbol_type="method",
                        signature=method_sig,
                        body=method_body,
                        line_start=method.lineno,
                        line_end=method.end_lineno or method.lineno,
                        docstring=ast.get_docstring(method) or "",
                        parent_name=node.name,
                        parent_signature=class_sig,
                    ))
            else:
                # 메서드가 없는 클래스는 전체를 단일 심볼로
                body = "".join(lines[node.lineno - 1 : node.end_lineno])
                symbols.append(CodeSymbol(
                    name=node.name,
                    symbol_type="class",
                    signature=class_sig,
                    body=body,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    docstring=class_doc,
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
    """키워드 감지 + 중괄호 매칭으로 심볼을 추출한다.

    Go: 리시버 메서드(func (s *Type) Method)는 parent_name으로 Type을 기록한다.
    Java/TS/JS: 클래스 내부 메서드를 개별 심볼로 추출하고 parent_name에 클래스명을 기록한다.
    """
    patterns = _DEFINITION_PATTERNS.get(language, [])
    if not patterns:
        return []

    lines = content.splitlines(keepends=True)
    symbols: list[CodeSymbol] = []
    used_ranges: list[tuple[int, int]] = []

    # Go 리시버 메서드 패턴: func (s *Type) Method(
    go_receiver_pattern = re.compile(
        r"^func\s+\(\s*\w+\s+\*?(\w+)\s*\)\s+(\w+)\s*\(",
    )

    for line_idx, line in enumerate(lines):
        # 이미 다른 심볼 범위에 포함된 라인은 건너뜀
        if any(s <= line_idx < e for s, e in used_ranges):
            continue

        stripped = line.strip()

        # Go 리시버 메서드를 먼저 확인
        if language == "go":
            recv_m = go_receiver_pattern.match(stripped)
            if recv_m:
                parent_type = recv_m.group(1)
                method_name = recv_m.group(2)
                block_start = sum(len(l) for l in lines[:line_idx])
                brace_pos = content.find("{", block_start)
                if brace_pos != -1:
                    block_end = _find_matching_brace(content, brace_pos)
                    body = content[block_start:block_end]
                    end_line = content[:block_end].count("\n") + 1
                    sig_end = body.find("{")
                    signature = body[:sig_end].strip() if sig_end != -1 else stripped
                    docstring = _extract_preceding_comment(lines, line_idx)

                    symbols.append(CodeSymbol(
                        name=method_name,
                        symbol_type="method",
                        signature=signature,
                        body=body,
                        line_start=line_idx + 1,
                        line_end=end_line,
                        docstring=docstring,
                        parent_name=parent_type,
                        parent_signature=f"type {parent_type} struct",
                    ))
                    used_ranges.append((line_idx, end_line))
                    continue

        for pattern, sym_type in patterns:
            m = pattern.match(stripped)
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
            signature = body[:sig_end].strip() if sig_end != -1 else stripped

            # docstring: 직전 줄의 주석
            docstring = _extract_preceding_comment(lines, line_idx)

            # Java/TS/JS 클래스는 내부 메서드를 개별 추출
            if sym_type == "class" and language in ("java", "typescript", "javascript"):
                inner_methods = _extract_class_methods(
                    body, language, name, signature, line_idx,
                )
                if inner_methods:
                    symbols.extend(inner_methods)
                else:
                    symbols.append(CodeSymbol(
                        name=name,
                        symbol_type=sym_type,
                        signature=signature,
                        body=body,
                        line_start=line_idx + 1,
                        line_end=end_line,
                        docstring=docstring,
                    ))
            else:
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


# 클래스 내부 메서드를 감지하는 패턴 (언어별)
_METHOD_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "java": [
        re.compile(
            r"^\s*(?:public|private|protected|static|final|synchronized|\s)*"
            r"[\w<>\[\],\s]+\s+(\w+)\s*\(",
        ),
    ],
    "typescript": [
        re.compile(r"^\s*(?:public|private|protected|static|readonly|\s)*(?:async\s+)?(\w+)\s*\("),
    ],
    "javascript": [
        re.compile(r"^\s*(?:static\s+)?(?:async\s+)?(\w+)\s*\("),
    ],
}


def _extract_class_methods(
    class_body: str,
    language: str,
    class_name: str,
    class_signature: str,
    class_line_offset: int,
) -> list[CodeSymbol]:
    """클래스 본문에서 메서드를 개별 심볼로 추출한다."""
    method_patterns = _METHOD_PATTERNS.get(language, [])
    if not method_patterns:
        return []

    # 클래스 본문에서 첫 번째 { 이후부터 파싱
    brace_pos = class_body.find("{")
    if brace_pos == -1:
        return []

    inner_content = class_body[brace_pos + 1 :]
    inner_lines = inner_content.splitlines(keepends=True)

    methods: list[CodeSymbol] = []
    used_ranges: list[tuple[int, int]] = []
    # inner_content가 class_body 내에서 시작하는 offset
    inner_offset = brace_pos + 1

    for line_idx, line in enumerate(inner_lines):
        if any(s <= line_idx < e for s, e in used_ranges):
            continue

        stripped = line.strip()
        # 클래스 이름과 같은 이름(생성자 등)은 건너뜀 — 필드 선언도 건너뜀
        if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
            continue

        for pattern in method_patterns:
            m = pattern.match(stripped)
            if not m:
                continue

            name = m.group(1)
            # 필드 선언이나 클래스/인터페이스 키워드는 건너뜀
            if name in ("class", "interface", "enum", "return", "if", "for",
                        "while", "switch", "try", "catch", "throw", "new"):
                continue

            # 메서드 블록 시작 위치 계산
            method_start_in_inner = sum(len(l) for l in inner_lines[:line_idx])
            method_start_in_body = inner_offset + method_start_in_inner
            method_brace = class_body.find("{", method_start_in_body)
            if method_brace == -1:
                continue

            method_end = _find_matching_brace(class_body, method_brace)
            method_body = class_body[method_start_in_body:method_end]
            method_end_line = class_body[:method_end].count("\n")

            # 시그니처
            sig_end = method_body.find("{")
            method_sig = method_body[:sig_end].strip() if sig_end != -1 else stripped

            # docstring: 직전 줄의 주석
            docstring = _extract_preceding_comment(inner_lines, line_idx)

            methods.append(CodeSymbol(
                name=name,
                symbol_type="method",
                signature=method_sig,
                body=method_body,
                line_start=class_line_offset + 1 + inner_content[:method_start_in_inner].count("\n") + 1,
                line_end=class_line_offset + 1 + method_end_line,
                docstring=docstring,
                parent_name=class_name,
                parent_signature=class_signature,
            ))
            used_ranges.append((line_idx, line_idx + method_body.count("\n") + 1))
            break

    return methods


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
