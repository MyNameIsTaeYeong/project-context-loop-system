"""AST 기반 코드 심볼 추출 모듈 테스트."""

from __future__ import annotations

import textwrap

import pytest

from context_loop.processor.ast_code_extractor import (
    CodeExtraction,
    CodeSymbol,
    extract_code_symbols,
    to_chunks,
    to_graph_data,
)
from context_loop.processor.graph_extractor import Entity, GraphData, Relation


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------


class TestPythonExtraction:
    """Python 코드 심볼 추출 테스트."""

    _PYTHON_CODE = textwrap.dedent("""\
        import os
        from pathlib import Path

        class MyService:
            \"\"\"서비스 클래스.\"\"\"

            def __init__(self, name: str) -> None:
                self.name = name

            def run(self) -> None:
                pass

        async def handle_request(ctx: dict, req: str) -> str:
            \"\"\"요청을 처리한다.\"\"\"
            return "ok"

        def helper() -> int:
            return 42
    """)

    def test_extracts_top_level_symbols(self) -> None:
        """최상위 함수/클래스를 추출한다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        assert result.language == "python"
        names = {s.name for s in result.symbols}
        assert "MyService" in names
        assert "handle_request" in names
        assert "helper" in names
        # 클래스 내부 메서드는 별도 심볼이 아님
        assert "__init__" not in names
        assert "run" not in names

    def test_symbol_types(self) -> None:
        """심볼 유형이 올바르게 설정된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        types = {s.name: s.symbol_type for s in result.symbols}
        assert types["MyService"] == "class"
        assert types["handle_request"] == "function"
        assert types["helper"] == "function"

    def test_extracts_imports(self) -> None:
        """import를 추출한다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        assert "os" in result.imports
        assert "pathlib" in result.imports

    def test_function_signature(self) -> None:
        """함수 시그니처가 올바르게 생성된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        handle = next(s for s in result.symbols if s.name == "handle_request")
        assert "async def" in handle.signature
        assert "ctx: dict" in handle.signature
        assert "-> str" in handle.signature

    def test_docstring_extraction(self) -> None:
        """독스트링을 추출한다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        svc = next(s for s in result.symbols if s.name == "MyService")
        assert "서비스 클래스" in svc.docstring

        handle = next(s for s in result.symbols if s.name == "handle_request")
        assert "요청을 처리" in handle.docstring

    def test_body_contains_full_code(self) -> None:
        """심볼 body에 전체 코드가 포함된다."""
        result = extract_code_symbols(self._PYTHON_CODE, "service.py")

        svc = next(s for s in result.symbols if s.name == "MyService")
        assert "class MyService" in svc.body
        assert "def __init__" in svc.body
        assert "def run" in svc.body

    def test_empty_content(self) -> None:
        """빈 내용은 빈 결과를 반환한다."""
        result = extract_code_symbols("", "empty.py")
        assert result.symbols == []
        assert result.imports == []

    def test_syntax_error_fallback(self) -> None:
        """파싱 실패 시 전체 파일로 대체한다."""
        broken = "def broken(\nclass what:"
        result = extract_code_symbols(broken, "broken.py")
        assert len(result.symbols) == 1
        assert result.symbols[0].symbol_type == "module"


# ---------------------------------------------------------------------------
# Go extractor
# ---------------------------------------------------------------------------


class TestGoExtraction:
    """Go 코드 심볼 추출 테스트."""

    _GO_CODE = textwrap.dedent("""\
        package main

        import (
            "context"
            "fmt"
            "github.com/example/vpc/service"
        )

        // HandleRequest handles incoming HTTP requests.
        func HandleRequest(ctx context.Context, req *Request) (*Response, error) {
            svc := service.New()
            return svc.Process(req)
        }

        type VPCService struct {
            repo Repository
            name string
        }

        // Create creates a new VPC.
        func (s *VPCService) Create(req CreateRequest) (*VPC, error) {
            if req.Name == "" {
                return nil, fmt.Errorf("name required")
            }
            return s.repo.Save(req)
        }

        type Repository interface {
            Save(req CreateRequest) (*VPC, error)
            Find(id string) (*VPC, error)
        }
    """)

    def test_extracts_functions(self) -> None:
        """Go 함수를 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        assert result.language == "go"
        names = {s.name for s in result.symbols}
        assert "HandleRequest" in names
        assert "Create" in names

    def test_extracts_types(self) -> None:
        """Go struct/interface를 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        types = {s.name: s.symbol_type for s in result.symbols}
        assert types["VPCService"] == "struct"
        assert types["Repository"] == "interface"

    def test_extracts_imports(self) -> None:
        """Go import를 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        assert "context" in result.imports
        assert "fmt" in result.imports
        assert "github.com/example/vpc/service" in result.imports

    def test_preceding_comment(self) -> None:
        """함수 직전 주석을 docstring으로 추출한다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        handle = next(s for s in result.symbols if s.name == "HandleRequest")
        assert "incoming HTTP requests" in handle.docstring

    def test_body_contains_full_code(self) -> None:
        """심볼 body에 전체 코드가 포함된다."""
        result = extract_code_symbols(self._GO_CODE, "handler.go")

        handle = next(s for s in result.symbols if s.name == "HandleRequest")
        assert "func HandleRequest" in handle.body
        assert "svc.Process(req)" in handle.body


# ---------------------------------------------------------------------------
# TypeScript extractor
# ---------------------------------------------------------------------------


class TestTypeScriptExtraction:
    """TypeScript 코드 심볼 추출 테스트."""

    _TS_CODE = textwrap.dedent("""\
        import { Request, Response } from 'express';
        import axios from 'axios';

        export class UserService {
            private baseUrl: string;

            constructor(url: string) {
                this.baseUrl = url;
            }

            async getUser(id: string): Promise<User> {
                return axios.get(`${this.baseUrl}/users/${id}`);
            }
        }

        export async function handleRequest(req: Request, res: Response) {
            const svc = new UserService("http://api");
            const user = await svc.getUser(req.params.id);
            res.json(user);
        }
    """)

    def test_extracts_symbols(self) -> None:
        """TypeScript 클래스/함수를 추출한다."""
        result = extract_code_symbols(self._TS_CODE, "user.ts")

        assert result.language == "typescript"
        names = {s.name for s in result.symbols}
        assert "UserService" in names
        assert "handleRequest" in names

    def test_extracts_imports(self) -> None:
        """TypeScript import를 추출한다."""
        result = extract_code_symbols(self._TS_CODE, "user.ts")

        assert "express" in result.imports
        assert "axios" in result.imports


# ---------------------------------------------------------------------------
# JavaScript extractor
# ---------------------------------------------------------------------------


class TestJavaScriptExtraction:
    """JavaScript 코드 심볼 추출 테스트."""

    def test_extracts_function_and_class(self) -> None:
        code = textwrap.dedent("""\
            const express = require('express');

            class Router {
                constructor() {
                    this.routes = [];
                }
            }

            function createApp() {
                return new Router();
            }
        """)
        result = extract_code_symbols(code, "app.js")

        assert result.language == "javascript"
        names = {s.name for s in result.symbols}
        assert "Router" in names
        assert "createApp" in names
        assert "express" in result.imports


# ---------------------------------------------------------------------------
# Java extractor
# ---------------------------------------------------------------------------


class TestJavaExtraction:
    """Java 코드 심볼 추출 테스트."""

    def test_extracts_class(self) -> None:
        code = textwrap.dedent("""\
            import java.util.List;
            import com.example.service.VPCService;

            public class VPCController {
                private final VPCService service;

                public VPCController(VPCService service) {
                    this.service = service;
                }

                public List<VPC> listVPCs() {
                    return service.findAll();
                }
            }
        """)
        result = extract_code_symbols(code, "VPCController.java")

        assert result.language == "java"
        names = {s.name for s in result.symbols}
        assert "VPCController" in names
        assert "java.util.List" in result.imports
        assert "com.example.service.VPCService" in result.imports


# ---------------------------------------------------------------------------
# Fallback (unknown language)
# ---------------------------------------------------------------------------


class TestFallback:
    """알 수 없는 언어의 폴백 테스트."""

    def test_unknown_extension_returns_whole_file(self) -> None:
        """알 수 없는 확장자는 전체 파일을 단일 심볼로 반환한다."""
        content = "SELECT * FROM users WHERE id = 1;"
        result = extract_code_symbols(content, "query.sql")

        assert result.language == "unknown"
        assert len(result.symbols) == 1
        assert result.symbols[0].symbol_type == "module"
        assert result.symbols[0].body == content

    def test_yaml_returns_whole_file(self) -> None:
        content = "key: value\nlist:\n  - item1"
        result = extract_code_symbols(content, "config.yaml")
        assert len(result.symbols) == 1


# ---------------------------------------------------------------------------
# to_chunks
# ---------------------------------------------------------------------------


class TestToChunks:
    """CodeExtraction → Chunk 변환 테스트."""

    def test_converts_symbols_to_chunks(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[
                CodeSymbol(
                    name="HandleRequest",
                    symbol_type="function",
                    signature="func HandleRequest(ctx context.Context)",
                    body="func HandleRequest(ctx context.Context) {\n    // ...\n}",
                    line_start=1,
                    line_end=3,
                ),
            ],
            imports=["context"],
        )
        chunks = to_chunks(extraction, "handler.go")

        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert "# File: handler.go" in chunks[0].content
        assert "func HandleRequest" in chunks[0].content
        assert chunks[0].section_path == "handler.go > HandleRequest"
        assert chunks[0].token_count > 0

    def test_empty_symbols_returns_empty(self) -> None:
        extraction = CodeExtraction(file_path="empty.py", language="python")
        chunks = to_chunks(extraction, "empty.py")
        assert chunks == []


# ---------------------------------------------------------------------------
# to_graph_data
# ---------------------------------------------------------------------------


class TestToGraphData:
    """CodeExtraction → GraphData 변환 테스트."""

    def test_creates_module_entity(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[],
            imports=[],
        )
        graph = to_graph_data(extraction, "handler.go")

        assert len(graph.entities) == 1
        assert graph.entities[0].name == "handler.go"
        assert graph.entities[0].entity_type == "module"

    def test_creates_symbol_entities(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[
                CodeSymbol(
                    name="HandleRequest",
                    symbol_type="function",
                    signature="func HandleRequest()",
                    body="...",
                    line_start=1,
                    line_end=3,
                ),
            ],
            imports=[],
        )
        graph = to_graph_data(extraction, "handler.go")

        assert len(graph.entities) == 2  # module + function
        func_entity = next(e for e in graph.entities if e.name == "HandleRequest")
        assert func_entity.entity_type == "function"
        assert func_entity.description == "func HandleRequest()"

    def test_creates_import_relations(self) -> None:
        extraction = CodeExtraction(
            file_path="handler.go",
            language="go",
            symbols=[],
            imports=["context", "fmt"],
        )
        graph = to_graph_data(extraction, "handler.go")

        assert len(graph.relations) == 2
        targets = {r.target for r in graph.relations}
        assert "context" in targets
        assert "fmt" in targets
        for r in graph.relations:
            assert r.source == "handler.go"
            assert r.relation_type == "imports"

    def test_no_calls_contains_relations(self) -> None:
        """calls, contains 등 LLM 전용 관계는 생성하지 않는다."""
        extraction = CodeExtraction(
            file_path="service.py",
            language="python",
            symbols=[
                CodeSymbol(
                    name="MyClass", symbol_type="class",
                    signature="class MyClass", body="...",
                    line_start=1, line_end=5,
                ),
                CodeSymbol(
                    name="helper", symbol_type="function",
                    signature="def helper()", body="...",
                    line_start=7, line_end=9,
                ),
            ],
            imports=["os"],
        )
        graph = to_graph_data(extraction, "service.py")

        relation_types = {r.relation_type for r in graph.relations}
        assert relation_types == {"imports"}


# ---------------------------------------------------------------------------
# 통합: extract → to_chunks → to_graph_data
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """추출 → 변환 통합 테스트."""

    def test_python_end_to_end(self) -> None:
        code = textwrap.dedent("""\
            from pathlib import Path

            class FileReader:
                def read(self, path: str) -> str:
                    return Path(path).read_text()

            def main() -> None:
                reader = FileReader()
                print(reader.read("test.txt"))
        """)
        extraction = extract_code_symbols(code, "reader.py")
        chunks = to_chunks(extraction, "reader.py")
        graph = to_graph_data(extraction, "reader.py")

        # 심볼 2개 (FileReader, main)
        assert len(extraction.symbols) == 2

        # 청크 2개
        assert len(chunks) == 2
        assert any("FileReader" in c.content for c in chunks)
        assert any("def main" in c.content for c in chunks)

        # 그래프: module + 2 symbols = 3 entities, 1 import relation
        assert len(graph.entities) == 3
        assert len(graph.relations) == 1
        assert graph.relations[0].target == "pathlib"

    def test_go_end_to_end(self) -> None:
        code = textwrap.dedent("""\
            package main

            import "fmt"

            func Hello(name string) string {
                return fmt.Sprintf("Hello, %s!", name)
            }
        """)
        extraction = extract_code_symbols(code, "hello.go")
        chunks = to_chunks(extraction, "hello.go")
        graph = to_graph_data(extraction, "hello.go")

        assert len(extraction.symbols) >= 1
        assert any("Hello" in c.content for c in chunks)
        assert any(r.target == "fmt" for r in graph.relations)
